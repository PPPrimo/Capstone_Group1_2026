"""Robot-side collision guard for the follower arm.

Runs in its own thread next to the follower, so it keeps working whatever the network does.
The guard is the only writer of Goal_Position: callers hand it the leader's pose with
set_target(), and it moves the follower there at `hz`.

Detection: each joint's Present_Current is averaged over `avg_s` seconds. A fast move gives a
short spike that barely moves the average; a collision gives a sustained rise. When any joint's
average passes its threshold, the follower is frozen where it is (Goal := Present), so it stops
pushing but keeps holding itself up (a category-2 controlled stop). Targets are then ignored.

Resume: back the leader's tripped joint off until it is `resume_back_deg` behind where the
follower stopped (measured from the follower, not the leader: at the trip the leader is ahead by
the tracking error, so releasing earlier would glide the follower straight back into the
obstacle), or call reset(). The follower then glides to the target with smoothstep easing,
peaking at `resume_speed` deg/s, with detection still active. The same glide runs when the first
target arrives, so the follower never snaps to the operator's pose.

If bus reads or writes fail `max_failures` ticks in a row, the guard stops and reports "fault".

LeaderLock (below) mirrors a stop on the leader arm: every joint except the tripped one is held
where it is, and the tripped joint can only move back, away from the obstacle. It only needs the
guard's status dict, so it works locally or with a status received over the network.

Limits (mA, 0.25 s average) were set on 2026-10-17 and checked on 2026-11-25 with a hold test on
the bench (local teleop, limits disabled, each follower joint held by hand 3-5 times while the leader
kept pushing): all 26 holds crossed their limit, none was missed. Per-joint notes are next to
k_currentLimit. "Normal" is the highest average seen in normal moves across the 2026-11-25 runs.
shoulder_lift and elbow_flex trip slowly on gentle holds (up to ~1.8 s) because their current
builds gradually; don't lower their limits to fix that (see the elbow note), add a stall condition.
Not yet checked with the network's ~10 Hz input: recheck with --guard-log on the Pi.
Current is converted at 6.5 mA per count, the commonly quoted STS3215 value (not confirmed).
"""

import csv
import logging
import threading
import time
from collections import deque
from pathlib import Path

logger = logging.getLogger(__name__)

k_maPerCount = 6.5

k_currentLimit = {
    # 2026-11-25 hold test: holds / peak while held / time to trip; normal-use max
    "shoulder_pan": 200,   # 5 holds, 1182-2210, trip 0.07-0.28 s; normal 80
    "shoulder_lift": 600,  # 4 holds, 2081-2311, trip 0.37-1.77 s; normal 406 (carries arm weight)
    "elbow_flex": 850,     # 5 holds, 1394-2264, trip 0.29-1.35 s; normal 399 (680 in fast moves, 10-17).
                           # Keep above ~800: holding the wrist loads the elbow to ~800, and a lower
                           # limit would blame the elbow, so the leader lock would free the wrong joint.
    "wrist_flex": 200,     # 4 holds, 421-849, trip 0.32-0.75 s; normal 120
    "wrist_roll": 150,     # 3 holds, 214-423, trip 0.04-0.20 s; normal 76
    "gripper": 400,        # 3 holds, 891-911, trip 0.04-0.10 s; normal 85; gripping an object reads ~150
}


def current_ma(raw):
    # Bit 15 is the direction bit on STS servos; only the magnitude matters here.
    return (int(raw) & 0x7FFF) * k_maPerCount


class CollisionGuard:
    def __init__(self, follower, current_limit=None, hz=50.0, avg_s=0.25, arm_delay=2.0,
                 resume_speed=40.0, resume_min_s=1.5, resume_back_deg=2.0, max_failures=10,
                 log_dir=None):
        self.follower = follower

        # Use the limits passed in, or the defaults above. dict() makes our own copy.
        if current_limit is None:
            current_limit = k_currentLimit
        self.k_currentLimit = dict(current_limit)

        # Joint names, in order: shoulder_pan, shoulder_lift, ...
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

        # One rolling window of recent current readings per joint. A deque with maxlen drops the
        # oldest reading automatically when a new one is added.
        n_avg = max(1, round(avg_s * hz))       # 0.25 s x 50 Hz = 12 readings
        self._hist = {}
        for m in self.motors:
            self._hist[m] = deque(maxlen=n_avg)

        # Shared with callers; always accessed under _lock.
        self._lock = threading.Lock()
        self._m_leaderPos = None
        self._reset = False
        self._m_followerPos = None
        # direction: +1/-1 = the way the tripped joint was pushing (blocked for the leader), 0 when not stopped.
        self._status = {"state": "holding", "joint": None, "direction": 0, "back_off_deg": 0.0, "trips": 0}

        # Guard-thread state.
        self._tripped = None     # joint that tripped
        self._c_holdPos = None   # follower pose held while stopped
        self._trip_sign = 0      # +1/-1: direction the tripped joint was pushing
        self._glide_from = None  # pose a glide started from; None when not gliding
        self._glide_t0 = 0.0     # time the glide started
        self._glide_s = 0.0      # glide duration
        self._started = False    # a target has arrived at least once

        self._stop = threading.Event()
        self._thread = None
        self._writer = None

    # ---- called from other threads / the asyncio loop ----

    def start(self):
        self._thread = threading.Thread(target=self._run, name="collision-guard", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def set_target(self, m_leaderPos):
        """Latest leader pose, {joint: deg} (gripper 0-100). Ignored while stopped."""
        with self._lock:
            self._m_leaderPos = dict(m_leaderPos)

    def reset(self):
        """Manual resume from a stop."""
        with self._lock:
            self._reset = True

    def status(self):
        with self._lock:
            return dict(self._status)

    def positions(self):
        """Follower pose from the guard's latest read, or None before the first read."""
        with self._lock:
            if self._m_followerPos is None:
                return None
            return dict(self._m_followerPos)

    # ---- guard thread ----

    def _run(self):
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
                try:
                    self._tick(tick - t0)
                    failures = 0
                except Exception as exc:  # bus hiccup: try again next tick, give up after max_failures in a row
                    failures = failures + 1
                    if failures >= self.max_failures:
                        logger.error("Collision guard stopped: bus keeps failing (%s)", exc)
                        with self._lock:
                            self._status["state"] = "fault"
                        return
                # Sleep for whatever is left of this tick, so the loop runs at a steady rate.
                time_used = time.monotonic() - tick
                time_left = self.period - time_used
                if time_left > 0:
                    time.sleep(time_left)
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
        self._glide_s = max(self.resume_min_s, 1.5 * biggest_gap / self.resume_speed)

    def _tick(self, t):
        # ---- 1. Read the follower ----
        m_followerPos = self.follower.get_position()
        raw_current = self.follower.get_current()
        m_motorCurrent = {}
        for m in raw_current:
            m_motorCurrent[m] = current_ma(raw_current[m])

        # ---- 2. Swap data with the other threads ----
        with self._lock:
            self._m_followerPos = m_followerPos

            # Latest leader pose; any joint missing from it holds where it is.
            if self._m_leaderPos is None:
                m_leaderPos = None
            else:
                m_leaderPos = {}
                for m in self.motors:
                    if m in self._m_leaderPos:
                        m_leaderPos[m] = self._m_leaderPos[m]
                    else:
                        m_leaderPos[m] = m_followerPos[m]

            manual = self._reset
            self._reset = False

        # ---- 3. Rolling average of each joint's current ----
        m_currentAvg = {}
        for m in self.motors:
            self._hist[m].append(m_motorCurrent[m])
            m_currentAvg[m] = sum(self._hist[m]) / len(self._hist[m])

        # ---- 4. Follower goal for this tick: frozen, holding (no target yet), gliding, or following ----
        if self._tripped is not None:
            c_followerPos = self._c_holdPos
        elif m_leaderPos is None:
            c_followerPos = m_followerPos
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
            self.follower.set_position(c_followerPos)

        # ---- 5. Trip check, or resume check while stopped ----
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
                    # The follower lags behind its command, so the sign of (goal - actual) is the way it was pushing.
                    if c_followerPos[m] - m_followerPos[m] >= 0:
                        self._trip_sign = 1
                    else:
                        self._trip_sign = -1
                    self._glide_from = None
                    self.follower.set_position(self._c_holdPos)  # goal = where it is: stop pushing, keep holding
                    logger.warning("COLLISION on %s: %.0f mA avg > %.0f - follower frozen",
                                   m, m_currentAvg[m], self.k_currentLimit[m])
                    with self._lock:
                        self._status["trips"] = self._status["trips"] + 1
        else:
            back_off = self._back_off(m_leaderPos)
            if manual or back_off < 0:
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
                    self._hist[m].clear()  # forget the collision's high readings

        # ---- 6. Publish status for the telemetry and the leader lock ----
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


class LeaderLock:
    """Freezes the leader arm while the follower is stopped.

    Every joint except the tripped one is held where it was at the trip (torque on, goal = that pose).
    The tripped joint gets a one-way wall at its trip angle: pushing further into the collision
    direction switches its torque on so it springs back to the wall; moving the other way is free.
    When the guard leaves "stopped", every joint goes limp again.

    Safeguards: Torque_Limit is capped at `torque_pct` on all leader joints (RAM, restored by close());
    a held joint lets go if its averaged current passes `give_way_ma`, or after `lean_s` of being pushed
    above `push_ma`. A joint that let go stays free until the stop ends (the wall joint re-arms once it
    is back on the free side). The servo firmware's overload protection sits underneath.

    Call update() from the thread that already reads the leader, so all leader bus traffic stays on it.
    """

    def __init__(self, leader, torque_pct=50, on_deg=0.5, off_deg=0.5, push_ma=150, lean_s=3.0,
                 give_way_ma=700, avg_n=12):
        self.leader = leader
        self.bus = leader.bus
        self.torque_pct = torque_pct    # holding strength while locked, % of max
        self.on_deg = on_deg            # wall engages this far past the wall
        self.off_deg = off_deg          # wall lets go this far back on the free side
        self.push_ma = push_ma          # current that counts as leaning on a held joint
        self.lean_s = lean_s            # seconds of leaning before a held joint lets go
        self.give_way_ma = give_way_ma  # current at which a held joint lets go at once

        self.motors = []
        for m in self.bus.motors:
            self.motors.append(m)

        self._hist = {}                 # recent current readings per joint
        for m in self.motors:
            self._hist[m] = deque(maxlen=avg_n)

        self._limits = None             # original Torque_Limit values, restored by close()
        self.joint = None               # tripped joint while locked, else None
        self.sign = 0                   # +1/-1: blocked direction of the tripped joint
        self.c_wallPos = None           # angle of the one-way wall
        self.engaged = set()            # joints with torque on
        self.let_go = set()             # joints released by give-way / lean timeout
        self._lean_since = {}           # joint -> time it started being leaned on

    def start(self):
        # Remember each joint's Torque_Limit, then cap it at torque_pct (1000 = 100%).
        self._limits = {}
        for m in self.motors:
            self._limits[m] = self.bus.read("Torque_Limit", m, normalize=False)
        for m in self.motors:
            self.bus.write("Torque_Limit", m, int(self.torque_pct * 10), normalize=False)
        return self

    def close(self):
        self.release()
        if self._limits is not None:
            for m in self._limits:
                self.bus.write("Torque_Limit", m, self._limits[m], normalize=False)

    def release(self):
        # Every joint limp again.
        if len(self.engaged) > 0:
            self.bus.disable_torque(list(self.engaged))
        if self.joint is not None:
            logger.info("Leader unlocked")
        self.engaged.clear()
        self.let_go.clear()
        self._lean_since.clear()
        for m in self.motors:
            self._hist[m].clear()
        self.joint = None

    def _hold(self, motors, goals):
        # Torque on for these joints, each held at its goal angle.
        for m in motors:
            self.bus.write("Goal_Position", m, goals[m])  # goal before torque, so nothing jumps
        self.bus.enable_torque(list(motors))
        for m in motors:
            self.engaged.add(m)

    def _free(self, m, reason=None, let_go=False):
        # Torque off for one joint.
        self.bus.disable_torque(m)
        self.engaged.discard(m)
        self._hist[m].clear()
        if m in self._lean_since:
            del self._lean_since[m]
        if let_go:
            self.let_go.add(m)
        if reason is not None:
            logger.warning("Leader %s released: %s", m, reason)

    def update(self, status, m_leaderPos, m_motorCurrent):
        """status: CollisionGuard.status(). m_leaderPos: leader {joint: deg}. m_motorCurrent: raw Present_Current."""

        # ---- 1. Not stopped: make sure the leader is free, then nothing else to do ----
        stopped = status.get("state") == "stopped" and status.get("joint") is not None
        if not stopped:
            if self.joint is not None:
                self.release()
            return

        # ---- 2. A new stop: lock every joint except the tripped one, at its current angle ----
        if status["joint"] != self.joint:
            self.release()
            self.joint = status["joint"]
            self.sign = status.get("direction")
            if not self.sign:
                self.sign = 1
            self.c_wallPos = m_leaderPos[self.joint]
            others = []
            for m in self.motors:
                if m != self.joint:
                    others.append(m)
            self._hold(others, m_leaderPos)
            logger.warning("Leader locked: only %s can move, and only away from the collision", self.joint)

        # ---- 3. Safety: let a held joint go if it is forced hard, or leaned on for too long ----
        now = time.monotonic()
        for m in list(self.engaged):  # a copy, because _free() removes joints from engaged
            self._hist[m].append(current_ma(m_motorCurrent[m]))
            m_currentAvg = sum(self._hist[m]) / len(self._hist[m])

            if m_currentAvg > self.give_way_ma:
                self._free(m, f"gave way at {m_currentAvg:.0f} mA", let_go=True)
            else:
                if m_currentAvg > self.push_ma:
                    if m not in self._lean_since:
                        self._lean_since[m] = now       # leaning started now
                else:
                    self._lean_since[m] = now           # not leaning: restart the clock
                if now - self._lean_since[m] > self.lean_s:
                    self._free(m, f"pushed for {self.lean_s:g} s", let_go=True)

        # ---- 4. One-way wall on the tripped joint ----
        j = self.joint
        past = (m_leaderPos[j] - self.c_wallPos) * self.sign  # >0 = pushed further into the collision
        if j in self.let_go and past < -self.off_deg:
            self.let_go.discard(j)  # back on the free side: the wall re-arms
        if j in self.engaged:
            if past < -self.off_deg:
                self._free(j)  # moved back to the free side
        elif j not in self.let_go and past > self.on_deg:
            self._hold([j], {j: self.c_wallPos})  # pushed past the wall: spring back to it
