"""Run a local leader-to-follower arm test without WebRTC."""

import argparse
import logging
from pathlib import Path
import threading
import time

from RobotFiles.Drivers.LeaderArm import LeaderTeleopConfig, Leader
from RobotFiles.Drivers.FollowerArm import FollowerRobotConfig, Follower
from RobotFiles.CollisionGuard import CollisionGuard, LeaderLock


k_readFreq = 100  # (Hz) rate the leader joints are read and the follower updated (RL-4.1)
leader_state = None  # Latest leader sample: m_leaderPos plus velocity and m_motorCurrent
leader_state_lock = threading.Lock()
stop_event = threading.Event()


def get_all_states(device):
    """Read position, velocity, and current for every motor on the bus."""
    positions = device.get_position()
    velocities = device.get_velocity()
    currents = device.get_current()
    return {
        "timestamp": time.time(),
        "motors": {
            motor: {
                "position": positions[motor],
                "velocity": velocities[motor],
                "current": currents[motor],
            }
            for motor in positions
        },
    }


def run_leader_local(device, guard, lock):
    global leader_state
    while not stop_event.is_set():
        state = get_all_states(device)
        with leader_state_lock:
            leader_state = state
        # Mirror a follower stop on the leader: lock every joint but the tripped one.
        # Pull the leader's angles and currents out of the reading we just took (no extra bus reads).
        m_leaderPos = {}
        m_motorCurrent = {}
        for motor, data in state["motors"].items():
            m_leaderPos[motor] = data["position"]
            m_motorCurrent[motor] = data["current"]
        try:
            lock.update(guard.status(), m_leaderPos, m_motorCurrent)
        except Exception as exc:  # a failed lock write must not stop teleop; the follower guard still holds
            logging.warning("Leader lock update failed: %s", exc)
        time.sleep(1 / k_readFreq)


def run_follower_local(guard):
    # The collision guard owns the follower's goals; this loop only passes it the leader pose
    while not stop_event.is_set():
        with leader_state_lock:
            state = leader_state
        if state is not None:
            m_leaderPos = {}
            for motor, data in state["motors"].items():
                m_leaderPos[motor] = data["position"]
            guard.set_target(m_leaderPos)
        time.sleep(1 / k_readFreq)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--leader-port", default="COM3", help="Leader arm serial port (default: COM3)")
    parser.add_argument("--follower-port", default="COM4", help="Follower arm serial port (default: COM4)")
    parser.add_argument("--guard-log", action="store_true", help="Write a collision-guard CSV to local_logs/")
    parser.add_argument("--lock-torque", type=float, default=50, help="Leader holding strength while locked, %% of max (default 50)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", force=True)

    leader = Leader(LeaderTeleopConfig(
        port=args.leader_port,
        id="PrimoLeader",
        calibration_dir=Path("RobotFiles/calibration/leader"),
    ))
    follower = Follower(FollowerRobotConfig(
        port=args.follower_port,
        id="PrimoFollower",
        calibration_dir=Path("RobotFiles/calibration/follower"),
    ))
    threads = []
    guard = None
    lock = None
    try:
        leader.connect()
        follower.connect()
        if args.guard_log:
            guard_log_dir = "local_logs"
        else:
            guard_log_dir = None
        guard = CollisionGuard(follower, log_dir=guard_log_dir).start()
        lock = LeaderLock(leader, torque_pct=args.lock_torque).start()
        threads = [
            threading.Thread(target=run_leader_local, args=(leader, guard, lock)),
            threading.Thread(target=run_follower_local, args=(guard,)),
        ]
        for thread in threads:
            thread.start()
        while all(thread.is_alive() for thread in threads):
            for thread in threads:
                thread.join(timeout=0.2)
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        stop_event.set()
        for thread in threads:
            if thread.ident is not None:
                thread.join(timeout=2.0)
        if lock is not None:
            try:
                lock.close()  # leader limp again, torque limits restored
            except Exception as exc:
                print(f"Leader lock cleanup failed: {exc}")
        if guard is not None:
            guard.stop()  # before disconnect, so nothing writes to a closed bus
        for device in (leader, follower):
            if device.is_connected:
                device.disconnect()
        print("Shutdown complete")


if __name__ == "__main__":
    main()
