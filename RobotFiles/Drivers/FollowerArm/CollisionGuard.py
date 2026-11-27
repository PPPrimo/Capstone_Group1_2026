"""Detect sustained motor current and hold the follower until the leader backs off."""

import csv
import logging
import threading
import time
from collections import deque
from pathlib import Path

logger = logging.getLogger(__name__)

k_currentLimit = {
    #all limits were determined through testing and measuring the torques when manually applying resistance to individual joints.
    "shoulder_pan": 200,
    "shoulder_lift": 600,
    "elbow_flex": 850,
    "wrist_flex": 200,
    "wrist_roll": 150,
    "gripper": 400,
}


def make_lead_state(positions):
    return {"motors": {motor: {"position": position} for motor, position in positions.items()}}


#for the follower arm, current limits cause a stop
class CollisionGuard:
    #this class is responsible for monitoring the follower arm's current and freezing it if any joint exceeds its limit.
    def __init__(self, follower, current_limit=None, hz=50.0, avg_s=0.25, arm_delay=2.0,
                 resume_speed=40.0, resume_min_s=1.5, resume_back_deg=2.0, max_failures=10,
                 log_dir=None):
        self.follower = follower

        #we pass in none in the case where we might need to put custom values depending on the case
        if current_limit is None:
            current_limit = k_currentLimit
        self.k_currentLimit = dict(current_limit)

        # assigns the motors to a list so we can iterate through them in order to assign the current limits to each joint.
        # This is important because we need to know which joint is over-torqued and needs to be frozen.
        self.motors = []
        for m in self.k_currentLimit:
            self.motors.append(m)

        self.period = 1.0 / hz                  # seconds between ticks
        self.arm_delay = arm_delay              # no tripping in the first seconds after start
        self.resume_speed = resume_speed        # peak glide speed, deg/s
        self.resume_min_s = resume_min_s        # shortest glide, s
        self.resume_back_deg = resume_back_deg  # how far past the stop the leader must back off
        self.max_failures = max_failures        # failed ticks in a row before "fault"
        self.log_dir = log_dir

        # One rolling window of recent current readings per joint. only the top 12 are kept
        n_avg = max(1, round(avg_s * hz))       # 0.25 s x 50 Hz = 12 readings
        self.history = {}
        for m in self.motors:
            self.history[m] = deque(maxlen=n_avg)

        # Shared with callers; always accessed under _lock.
        self._lock = threading.Lock()
        self._pending_leader_state = None
        self._m_leaderPos = None
        self._reset = False
        self._m_followerPos = None
        self._lead_state = None
        # direction: +1/-1 = the way the tripped joint was pushing (blocked for the leader), 0 when not stopped.
        self._status = {"state": "holding", "joint": None, "direction": 0, "back_off_deg": 0.0, "trips": 0}

        # information about the joint that tripped in the follower arm
        self._tripped = None     # joint that tripped
        self._c_holdPos = None   # follower pose held while stopped
        self._trip_sign = 0      # +1/-1: direction the tripped joint was pushing
        self._glide_from = None  # pose a glide started from; None when not gliding
        self._glide_t0 = 0.0     # time the glide started
        self._glide_s = 0.0      # glide duration
        self._started = False    # a target has arrived at least once

        # Keep a guard decision and a motor write from interleaving. The WebRTC
        # tasks use only _lock, so they never wait for a serial transaction.
        self._motion_lock = threading.Lock()
        self._pending_goal = None
        self._applied_goal = None
        self._command_ready = threading.Event()
        self._stop = threading.Event()
        self._position_thread = None
        self._command_thread = None
        self._writer = None

    # ---- called from other threads / the asyncio loop ----

    def start(self):
        self._command_thread = threading.Thread(
            target=self._run_motor_commands, name="follower-motor-command", daemon=True,
        )
        self._position_thread = threading.Thread(
            target=self._run_position_guard, name="follower-position-guard", daemon=True,
        )
        self._command_thread.start()
        self._position_thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._command_ready.set()
        for worker in (self._position_thread, self._command_thread):
            if worker is not None and worker.ident is not None:
                worker.join()

    def submit_leader_state(self, leader_state):
        # WebRTC hands off its decoded packet. The command worker extracts the joints.
        with self._lock:
            self._pending_leader_state = leader_state
        self._command_ready.set()

    def set_target(self, m_leaderPos):
        #Latest leader pose, {joint: deg} (gripper 0-100). Ignored while stopped.
        #so we know where to move to
        with self._lock:
            self._m_leaderPos = dict(m_leaderPos)

    def reset(self):
        #allows for a manual resume
        with self._lock:
            self._reset = True

    def status(self):
        #returns the current status of the guard.
        with self._lock:
            return dict(self._status)

    def positions(self):
        #Follower pose from the guard's latest read, or None before the first read.
        with self._lock:
            if self._m_followerPos is None:
                return None
            return dict(self._m_followerPos)

    def lead_state(self):
        """Return the latest packet-ready motor snapshot. Callers treat it as read-only."""
        with self._lock:
            return self._lead_state

    def _run_motor_commands(self):
        failures = 0
        while not self._stop.is_set():
            self._command_ready.wait()
            self._command_ready.clear()
            with self._lock:
                leader_state = self._pending_leader_state
                self._pending_leader_state = None
            if leader_state is not None:
                try:
                    motors = leader_state["motors"]
                    self.set_target({motor: motors[motor]["position"] for motor in self.motors})
                except (KeyError, TypeError):
                    logger.warning("Ignoring incomplete or invalid leader state")

            with self._motion_lock:
                if self._stop.is_set():
                    return
                goal = self._pending_goal
                self._pending_goal = None
                if goal is None:
                    continue
                try:
                    self.follower.set_position(goal)
                except Exception as exc:
                    failures += 1
                    if failures >= self.max_failures:
                        self._set_fault("motor writes", exc)
                        return
                else:
                    failures = 0
                    self._applied_goal = goal

    def _set_fault(self, operation, error):
        # Called with _motion_lock held, so no goal can be written after a fault.
        logger.error("Collision guard stopped: %s keep failing (%s)", operation, error)
        with self._lock:
            self._status["state"] = "fault"
        self._stop.set()
        self._command_ready.set()

    def _run_position_guard(self):
        # Optional CSV log, one row per tick.
        log_file = None
        if self.log_dir is not None:
            path = Path(self.log_dir) / f"guard_{time.strftime('%Y%m%d_%H%M%S')}.csv"
            path.parent.mkdir(parents=True, exist_ok=True)
            log_file = open(path, "w", newline="")
            self._writer = csv.writer(log_file)
            header = ["t", "state", "tripped"]
            for m in self.motors:
                header.append(f"{m}.target")
                header.append(f"{m}.goal")
                header.append(f"{m}.pos")
                header.append(f"{m}.current_ma")
                header.append(f"{m}.current_avg")
            self._writer.writerow(header)
            logger.info("Collision guard logging to %s", path)

        t0 = time.monotonic()
        failures = 0
        try:
            while not self._stop.is_set():
                tick = time.monotonic()
                with self._motion_lock:
                    if self._stop.is_set():
                        return
                    if self._tripped is None:
                        self._pending_goal = None
                    try:
                        self._tick(tick - t0)
                        failures = 0
                    except Exception as exc:
                        self._pending_goal = None
                        failures += 1
                        if failures >= self.max_failures:
                            self._set_fault("position guard ticks", exc)
                            return
                    if self._tripped is not None and self._applied_goal is not self._c_holdPos:
                        self._pending_goal = self._c_holdPos
                    if self._pending_goal is not None:
                        self._command_ready.set()
                # Sleep for whatever is left of this tick, so the loop runs at a steady rate.
                time_used = time.monotonic() - tick
                self._stop.wait(max(0, self.period - time_used))
        finally:
            if log_file is not None:
                log_file.close()

    def _glide_to(self, start, m_leaderPos, t):
        # Start a glide from `start` towards the leader pose.
        self._glide_from = dict(start)
        self._glide_t0 = t

        # The glide takes long enough that the joint with the biggest gap stays under resume_speed.
        # Smoothstep peaks at 1.5x its average speed, hence the 1.5.
        biggest_gap = 0.0
        for m in self.motors:
            gap = abs(m_leaderPos[m] - start[m])
            if gap > biggest_gap:
                biggest_gap = gap
        self._glide_s = max(self.resume_min_s, 1.5 * biggest_gap / self.resume_speed) #how long the glide should take, based on the biggest gap and the resume speed

    def _tick(self, t):
        # read the followers data
        m_followerPos = self.follower.get_position()
        m_motorCurrent = self.follower.get_current()
        lead_state = make_lead_state(m_followerPos)

        # changing the data that is shared with other threads, so we need to lock it to avoid race conditions
        with self._lock:
            self._m_followerPos = m_followerPos
            self._lead_state = lead_state

            # Our own copy of the latest leader pose for this tick (every leader message has all 6 joints).
            if self._m_leaderPos is None: #only if no leader pos has arrived yet (aka the beginning)
                m_leaderPos = None
            else:
                m_leaderPos = {}
                for m in self.motors:
                    m_leaderPos[m] = self._m_leaderPos[m]


            manual = self._reset
            if self._tripped is None or self._applied_goal is self._c_holdPos:
                self._reset = False

        # rolling average for each joints current
        m_currentAvg = {}
        for m in self.motors:
            self.history[m].append(m_motorCurrent[m])
            m_currentAvg[m] = sum(self.history[m]) / len(self.history[m])

        # What the follower is doing each tick: frozen (stop moving), holding (stay stopped), gliding to leader (after collision), or following leader (no collision)
        was_tripped = self._tripped is not None
        if self._tripped is not None:
            c_followerPos = self._c_holdPos
        elif m_leaderPos is None:
            c_followerPos = m_followerPos #just staarted leader hasnt moved
        else:
            if not self._started:
                self._started = True
                self._glide_to(m_followerPos, m_leaderPos, t)  # first target: glide, don't snap
            c_followerPos = m_leaderPos
            if self._glide_from is not None:
                a = min(1.0, (t - self._glide_t0) / self._glide_s)   # glide progress, 0 to 1
                s = a * a * (3 - 2 * a)                               # smoothstep: zero speed at both ends
                c_followerPos = {}
                for m in self.motors:
                    c_followerPos[m] = self._glide_from[m] + s * (m_leaderPos[m] - self._glide_from[m])
                if a >= 1.0:
                    self._glide_from = None

        # is it over torqued if sso than trip it and freeze follower arm.
        if self._tripped is None:
            if t > self.arm_delay:
                # Find the joint furthest over its limit, measured as a fraction of that limit.
                worst = None
                worst_ratio = 1.0
                for m in self.motors:
                    ratio = m_currentAvg[m] / self.k_currentLimit[m]
                    if ratio > worst_ratio:
                        worst = m
                        worst_ratio = ratio

                if worst is not None:
                    m = worst
                    self._tripped = m
                    self._c_holdPos = dict(m_followerPos)
                    # Use the goal actually written to estimate which way the motor was pushing.
                    last_goal = self._applied_goal if self._applied_goal is not None else c_followerPos
                    if last_goal[m] - m_followerPos[m] >= 0:
                        self._trip_sign = 1
                    else:
                        self._trip_sign = -1
                    self._glide_from = None
                    logger.warning("COLLISION on %s: %.0f mA avg > %.0f - follower frozen",
                                   m, m_currentAvg[m], self.k_currentLimit[m])
                    with self._lock:
                        self._status["trips"] = self._status["trips"] + 1
        else:
            back_off = self._back_off(m_leaderPos)
            if self._applied_goal is self._c_holdPos and (manual or back_off < 0):
                if manual:
                    logger.info("Collision guard resuming (manual reset)")
                else:
                    logger.info("Collision guard resuming (leader backed off)")
                if m_leaderPos is None:
                    glide_target = self._c_holdPos
                else:
                    glide_target = m_leaderPos
                self._glide_to(self._c_holdPos, glide_target, t)
                self._tripped = None
                self._c_holdPos = None
                for m in self.motors:
                    self.history[m].clear()  # forget the collision's high readings

        if not was_tripped and self._tripped is None and m_leaderPos is not None:
            self._pending_goal = c_followerPos

        #Publish status for the telemetry and the leader
        j = self._tripped
        if j is not None:
            state = "stopped"
            direction = self._trip_sign
            back_off = max(0.0, self._back_off(m_leaderPos))
        else:
            direction = 0
            back_off = 0.0
            if m_leaderPos is None:
                state = "holding"
            elif self._glide_from is not None:
                state = "gliding"
            elif t > self.arm_delay:
                state = "following"
            else:
                state = "arming"
        with self._lock:
            self._status["state"] = state
            self._status["joint"] = j
            self._status["direction"] = direction
            self._status["back_off_deg"] = round(back_off, 1)

        # ---- 7. Log one row ----
        if self._writer is not None:
            if j is None:
                tripped_text = ""
            else:
                tripped_text = j
            row = [f"{t:.3f}", state, tripped_text]
            for m in self.motors:
                if m_leaderPos is None:
                    row.append("")
                else:
                    row.append(f"{m_leaderPos[m]:.2f}")
                row.append(f"{c_followerPos[m]:.2f}")
                row.append(f"{m_followerPos[m]:.2f}")
                row.append(f"{m_motorCurrent[m]:.0f}")
                row.append(f"{m_currentAvg[m]:.0f}")
            self._writer.writerow(row)

    def _back_off(self, m_leaderPos):
        # Degrees the leader's tripped joint still has to back off. Above 0: keep going. Below 0: far enough.
        j = self._tripped
        if m_leaderPos is None:
            leader = self._c_holdPos[j]
        else:
            leader = m_leaderPos[j]
        return (leader - self._c_holdPos[j]) * self._trip_sign + self.resume_back_deg
