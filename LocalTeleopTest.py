"""Run a local leader-to-follower arm test without WebRTC."""

import argparse
from pathlib import Path
import threading
import time

from RobotFiles.Drivers.LeaderArm import LeaderTeleopConfig, Leader
from RobotFiles.Drivers.FollowerArm import FollowerRobotConfig, Follower


EXECUTION_PERIOD = 0.01
payload = None
payload_lock = threading.Lock()
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


def run_leader_local(device):
    global payload
    while not stop_event.is_set():
        states = get_all_states(device)
        with payload_lock:
            payload = states
        time.sleep(EXECUTION_PERIOD)


def run_follower_local(device):
    while not stop_event.is_set():
        with payload_lock:
            states = payload
        print(states)
        if states is not None:
            device.send_action({f"{motor}.pos": data["position"] for motor, data in states["motors"].items()})
        time.sleep(EXECUTION_PERIOD)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--leader-port", default="COM3", help="Leader arm serial port (default: COM3)")
    parser.add_argument("--follower-port", default="COM4", help="Follower arm serial port (default: COM4)")
    args = parser.parse_args()

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
    try:
        leader.connect()
        follower.connect()
        threads = [
            threading.Thread(target=run_leader_local, args=(leader,)),
            threading.Thread(target=run_follower_local, args=(follower,)),
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
        for device in (leader, follower):
            if device.is_connected:
                device.disconnect()
        print("Shutdown complete")


if __name__ == "__main__":
    main()
