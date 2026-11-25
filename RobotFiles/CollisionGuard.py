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

Thresholds (mA, 0.25 s average) come from the 2026-10-17 bench runs with 100 Hz local teleop.
shoulder_pan is well tested; shoulder_lift, elbow_flex and gripper have one real trip each;
the wrist joints are untested. Recheck them with the network's ~10 Hz input using the CSV log.
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
    "shoulder_pan": 200,   # blocked avg 381, normal max 93
    "shoulder_lift": 600,  # one real trip at 604; normal reaches ~470 (carries arm weight)
    "elbow_flex": 850,     # one real trip at 861; normal reaches ~680
    "wrist_flex": 200,     # untested: normal max 83
    "wrist_roll": 150,     # untested: normal max 8
    "gripper": 400,        # high on purpose: holding an object reads ~150 steady
}


def current_ma(raw):
    # Bit 15 is the direction bit on STS servos; only the magnitude matters here.
    return (int(raw) & 0x7FFF) * k_maPerCount


class CollisionGuard:
    def __init__(self, follower, current_limit=None, *, hz=50.0, avg_s=0.25, arm_delay=2.0,
                 resume_speed=40.0, resume_min_s=1.5, resume_back_deg=2.0, max_failures=10,
                 log_dir=None):
        self.follower = follower
        self.k_currentLimit = dict(current_limit or k_currentLimit)
        self.motors = list(self.k_currentLimit)
        self.period = 1.0 / hz
        self.arm_delay = arm_delay
        self.resume_speed = resume_speed
        self.resume_min_s = resume_min_s
        self.resume_back_deg = resume_back_deg
        self.max_failures = max_failures
        self.log_dir = log_dir
        n_avg = max(1, round(avg_s * hz))
        self._hist = {m: deque(maxlen=n_avg) for m in self.motors}

        # Shared with callers; always accessed under _lock.
        self._lock = threading.Lock()
        self._m_leaderPos = None
        self._reset = False
        self._m_followerPos = None
        # direction: +1/-1 = the way the tripped joint was pushing (blocked for the leader), 0 when not stopped.
        self._status = {"state": "holding", "joint": None, "direction": 0, "back_off_deg": 0.0, "trips": 0}

        # Guard-thread state.
        self._tripped = None     # joint that tripped
        self._c_holdPos = None      # follower pose held while stopped
        self._trip_sign = 0      # +1/-1: direction the tripped joint was pushing
        self._glide_from = None  # pose a glide started from; None when not gliding
        self._glide_t0 = self._glide_s = 0.0
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
            return None if self._m_followerPos is None else dict(self._m_followerPos)

    # ---- guard thread ----

    def _run(self):
        log_file = None
        if self.log_dir is not None:
            path = Path(self.log_dir) / f"guard_{time.strftime('%Y%m%d_%H%M%S')}.csv"
            path.parent.mkdir(parents=True, exist_ok=True)
            log_file = open(path, "w", newline="")
            self._writer = csv.writer(log_file)
            self._writer.writerow(["t", "state", "tripped"] + [
                f"{m}.{k}" for m in self.motors for k in ("target", "goal", "pos", "current_ma", "current_avg")])
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
                    failures += 1
                    if failures >= self.max_failures:
                        logger.error("Collision guard stopped: bus keeps failing (%s)", exc)
                        with self._lock:
                            self._status["state"] = "fault"
                        return
                time.sleep(max(0.0, self.period - (time.monotonic() - tick)))
        finally:
            if log_file is not None:
                log_file.close()

    def _glide_to(self, start, m_leaderPos, t):
        # Smoothstep peaks at 1.5x its average speed, so size the duration to keep the peak under the cap.
        self._glide_from, self._glide_t0 = dict(start), t
        self._glide_s = max(self.resume_min_s,
                            1.5 * max(abs(m_leaderPos[m] - start[m]) for m in self.motors) / self.resume_speed)

    def _tick(self, t):
        m_followerPos = self.follower.get_position()
        m_motorCurrent = {m: current_ma(v) for m, v in self.follower.get_current().items()}
        with self._lock:
            self._m_followerPos = m_followerPos
            m_leaderPos = None if self._m_leaderPos is None else {m: self._m_leaderPos.get(m, m_followerPos[m]) for m in self.motors}
            manual, self._reset = self._reset, False
        for m in self.motors:
            self._hist[m].append(m_motorCurrent[m])
        m_currentAvg = {m: sum(self._hist[m]) / len(self._hist[m]) for m in self.motors}

        # Follower goal for this tick: frozen, holding (no target yet), gliding, or following.
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
                a = min(1.0, (t - self._glide_t0) / self._glide_s)
                s = a * a * (3 - 2 * a)  # smoothstep: zero speed at both ends
                c_followerPos = {m: self._glide_from[m] + s * (m_leaderPos[m] - self._glide_from[m]) for m in self.motors}
                if a >= 1.0:
                    self._glide_from = None
            self.follower.set_position(c_followerPos)

        if self._tripped is None:
            if t > self.arm_delay:
                hit = [m for m in self.motors if m_currentAvg[m] > self.k_currentLimit[m]]
                if hit:
                    m = max(hit, key=lambda k: m_currentAvg[k] / self.k_currentLimit[k])
                    self._tripped, self._c_holdPos = m, dict(m_followerPos)
                    self._trip_sign = 1 if c_followerPos[m] - m_followerPos[m] >= 0 else -1  # follower lags behind its command
                    self._glide_from = None
                    self.follower.set_position(self._c_holdPos)
                    logger.warning("COLLISION on %s: %.0f mA avg > %.0f - follower frozen",
                                   m, m_currentAvg[m], self.k_currentLimit[m])
                    with self._lock:
                        self._status["trips"] += 1
        else:
            j = self._tripped
            leader = (m_leaderPos or self._c_holdPos)[j]
            back_off = (leader - self._c_holdPos[j]) * self._trip_sign + self.resume_back_deg  # >0: keep backing off
            if manual or back_off < 0:
                logger.info("Collision guard resuming (%s)", "manual reset" if manual else "leader backed off")
                self._glide_to(self._c_holdPos, m_leaderPos or self._c_holdPos, t)
                self._tripped = self._c_holdPos = None
                for h in self._hist.values():
                    h.clear()  # forget the collision's high readings

        j = self._tripped
        if j is not None:
            leader = (m_leaderPos or self._c_holdPos)[j]
            back_off = max(0.0, (leader - self._c_holdPos[j]) * self._trip_sign + self.resume_back_deg)
            state = "stopped"
        else:
            back_off = 0.0
            state = ("holding" if m_leaderPos is None else "gliding" if self._glide_from is not None
                     else "following" if t > self.arm_delay else "arming")
        with self._lock:
            self._status.update(state=state, joint=j, direction=self._trip_sign if j else 0,
                                back_off_deg=round(back_off, 1))

        if self._writer is not None:
            self._writer.writerow([f"{t:.3f}", state, j or ""] + [x for m in self.motors for x in (
                "" if m_leaderPos is None else f"{m_leaderPos[m]:.2f}", f"{c_followerPos[m]:.2f}", f"{m_followerPos[m]:.2f}",
                f"{m_motorCurrent[m]:.0f}", f"{m_currentAvg[m]:.0f}")])


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

    def __init__(self, leader, *, torque_pct=50, on_deg=0.5, off_deg=0.5, push_ma=150, lean_s=3.0,
                 give_way_ma=700, avg_n=12):
        self.leader = leader
        self.bus = leader.bus
        self.torque_pct = torque_pct
        self.on_deg, self.off_deg = on_deg, off_deg
        self.push_ma, self.lean_s, self.give_way_ma = push_ma, lean_s, give_way_ma
        self.motors = list(self.bus.motors)
        self._hist = {m: deque(maxlen=avg_n) for m in self.motors}
        self._limits = None
        self.joint = None        # tripped joint while locked, else None
        self.sign = 0
        self.c_wallPos = None
        self.engaged = set()     # joints with torque on
        self.let_go = set()      # joints released by give-way / lean timeout
        self._lean_since = {}

    def start(self):
        self._limits = {m: self.bus.read("Torque_Limit", m, normalize=False) for m in self.motors}
        for m in self.motors:
            self.bus.write("Torque_Limit", m, int(self.torque_pct * 10), normalize=False)
        return self

    def close(self):
        self.release()
        if self._limits is not None:
            for m, v in self._limits.items():
                self.bus.write("Torque_Limit", m, v, normalize=False)

    def release(self):
        if self.engaged:
            self.bus.disable_torque(list(self.engaged))
        if self.joint is not None:
            logger.info("Leader unlocked")
        self.engaged.clear()
        self.let_go.clear()
        self._lean_since.clear()
        for h in self._hist.values():
            h.clear()
        self.joint = None

    def _hold(self, motors, goals):
        for m in motors:
            self.bus.write("Goal_Position", m, goals[m])  # goal before torque, so nothing jumps
        self.bus.enable_torque(list(motors))
        self.engaged.update(motors)

    def _free(self, m, reason=None, let_go=False):
        self.bus.disable_torque(m)
        self.engaged.discard(m)
        self._hist[m].clear()
        self._lean_since.pop(m, None)
        if let_go:
            self.let_go.add(m)
        if reason:
            logger.warning("Leader %s released: %s", m, reason)

    def update(self, status, m_leaderPos, m_motorCurrent):
        """status: CollisionGuard.status(). positions: leader {joint: deg}. currents_raw: leader Present_Current."""
        if status.get("state") != "stopped" or not status.get("joint"):
            if self.joint is not None:
                self.release()
            return
        if status["joint"] != self.joint:  # new stop: lock everything at the current pose
            self.release()
            self.joint, self.sign = status["joint"], status.get("direction") or 1
            self.c_wallPos = m_leaderPos[self.joint]
            self._hold([m for m in self.motors if m != self.joint], m_leaderPos)
            logger.warning("Leader locked: only %s can move, and only away from the collision", self.joint)

        now = time.monotonic()
        for m in list(self.engaged):
            self._hist[m].append(current_ma(m_motorCurrent[m]))
            m_currentAvg = sum(self._hist[m]) / len(self._hist[m])
            if m_currentAvg > self.give_way_ma:
                self._free(m, f"gave way at {m_currentAvg:.0f} mA", let_go=True)
                continue
            self._lean_since[m] = self._lean_since.get(m, now) if m_currentAvg > self.push_ma else now
            if now - self._lean_since[m] > self.lean_s:
                self._free(m, f"pushed for {self.lean_s:g} s", let_go=True)

        j = self.joint
        past = (m_leaderPos[j] - self.c_wallPos) * self.sign  # >0 = pushed further into the collision
        if j in self.let_go and past < -self.off_deg:
            self.let_go.discard(j)  # back on the free side: the wall re-arms
        if j in self.engaged:
            if past < -self.off_deg:
                self._free(j)
        elif j not in self.let_go and past > self.on_deg:
            self._hold([j], {j: self.c_wallPos})
