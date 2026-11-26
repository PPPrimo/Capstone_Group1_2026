"""
This file describes the collision process for the robotic arms and what happens when they bump into something. 
The purpose of this file has two components:
1. To program so that the follower arm stops if any joints reach over, drawing a certain current over a set period of time. 
The whole follower arm freezes in that position until the leader arm moves in a direction that is against the point of resistance,
allowing the arm to move freely again.
2. For how the detection works: when the follower arm's Present_Current is averaged out over a period of seconds,
that means a fast movement will cause a spike but won't trigger the threshold to hit. It has to be a sustained spike.
Then that specific joint that gets over-torqued, so the whole arm freezes, but all joints get frozen on the follower arm.
In order to resume, the leader's tripped joint needs to go in the opposite rotation of the direction that caused the freezing,
and then the follower will glide to the new target that the leader is in and start following again.
Additional implementation is the LeaderLock, where, when the follower freezes because it collides,
all joints on the follow-on leader freeze as well (except that, in this unique case,
the joint that actually is being over-torqued and gets frozen on the leader arm).
You can only move that joint in the rotation that will unlock both the follower and the leader to the beginning.
This allows the user to see exactly which area was impacted and easily navigate into an orientation that will stop from being blocked.
All of the limits were determined via testing and measuring the torques when manually applying resistance to individual joints.

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
    #all limits were determined through testing and measuring the torques when manually applying resistance to individual joints.
    "shoulder_pan": 200,   
    "shoulder_lift": 600,  
    "elbow_flex": 850,              
    "wrist_flex": 200,     
    "wrist_roll": 150,     
    "gripper": 400,       
}


def current_ma(raw):
    #measures the current in mA from the raw Present_Current value, which is a 16-bit signed integer.
    return (int(raw) & 0x7FFF) * k_maPerCount

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
        self._m_leaderPos = None
        self._reset = False
        self._m_followerPos = None
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

        self._stop = threading.Event()
        self._thread = None
        self._writer = None

    # ---- called from other threads / the asyncio loop ----

    def start(self):
        self._thread = threading.Thread(target=self._run, name="collision-guard", daemon=True)
        self._thread.start() #strating running the _run function in the background
        return self

    #stopps the thread allows for clean exiting of the program
    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

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
        self._glide_s = max(self.resume_min_s, 1.5 * biggest_gap / self.resume_speed) #how long the glide should take, based on the biggest gap and the resume speed

    def _tick(self, t):
        # read the followers data
        m_followerPos = self.follower.get_position()
        raw_current = self.follower.get_current()
        m_motorCurrent = {}
        for m in raw_current:
            m_motorCurrent[m] = current_ma(raw_current[m])

        # changing the data that is shared with other threads, so we need to lock it to avoid race conditions
        with self._lock:
            self._m_followerPos = m_followerPos

            # Our own copy of the latest leader pose for this tick (every leader message has all 6 joints).
            if self._m_leaderPos is None: #only if no leader pos has arrived yet (aka the beginning)
                m_leaderPos = None
            else:
                m_leaderPos = {}
                for m in self.motors:
                    m_leaderPos[m] = self._m_leaderPos[m]
                    

            manual = self._reset
            self._reset = False

        # rolling average for each joints current
        m_currentAvg = {}
        for m in self.motors:
            self.history[m].append(m_motorCurrent[m])
            m_currentAvg[m] = sum(self.history[m]) / len(self.history[m])

        # What the follower is doing each tick: frozen (stop moving), holding (stay stopped), gliding to leader (after collision), or following leader (no collision)
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
            self.follower.set_position(c_followerPos)

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
                    self.history[m].clear()  # forget the collision's high readings

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


class LeaderLock:
    """
    The purpose of this class is to define how we freeze the leader arm when the follower arm has a collision. 
    Every joint except for the trip one will be held in frozen split with torque on at that pose. 
    The trip joint gets a one-way wall against the trip angle where the collision happens, so it can only go in the opposite direction. 
    It basically springs against the wall, but moving the other way is free. There's one safeguard in place: you can still move the leader arm. 
    It just has heavy resistance against any movement in any of the other directions. 

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

        self.history = {}                 # recent current readings per joint
        for m in self.motors:
            self.history[m] = deque(maxlen=avg_n)

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
            self.history[m].clear()
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
        self.history[m].clear()
        if m in self._lean_since:
            del self._lean_since[m]
        if let_go:
            self.let_go.add(m)
        if reason is not None:
            logger.warning("Leader %s released: %s", m, reason)

    def update(self, status, m_leaderPos, m_motorCurrent):
        #checkt the status of the leader arm and updates the torque on each joint accordingly.
        
        stopped = status.get("state") == "stopped" and status.get("joint") is not None
        # if not stopped leader joints should move freely
        if not stopped:
            if self.joint is not None:
                self.release()
            return

        # if stopped lock all joints except the torqued out one in the opposite direction
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

        # Allows the leader to be pushed past the wall without breaking
        now = time.monotonic()
        for m in list(self.engaged):  # a copy, because _free() removes joints from engaged
            self.history[m].append(current_ma(m_motorCurrent[m]))
            m_currentAvg = sum(self.history[m]) / len(self.history[m])

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

        # the one way wall for the over torqued joint
        j = self.joint
        past = (m_leaderPos[j] - self.c_wallPos) * self.sign  # >0 = pushed further into the collision
        if j in self.let_go and past < -self.off_deg:
            self.let_go.discard(j)  # back on the free side: the wall re-arms
        if j in self.engaged:
            if past < -self.off_deg:
                self._free(j)  # moved back to the free side
        elif j not in self.let_go and past > self.on_deg:
            self._hold([j], {j: self.c_wallPos})  # pushed past the wall: spring back to it
