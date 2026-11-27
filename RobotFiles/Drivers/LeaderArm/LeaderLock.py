"""Mirror a follower collision stop through torque feedback on the leader."""

import logging
import time
from collections import deque

logger = logging.getLogger(__name__)


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
        """Apply the follower's stop status using leader current magnitudes in milliamps."""

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
            self.history[m].append(m_motorCurrent[m])
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
