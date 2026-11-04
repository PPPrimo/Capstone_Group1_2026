

from RobotFiles.Drivers.FollowerArm import FollowerRobotConfig, Follower
from RobotFiles.Drivers.LeaderArm import LeaderTeleopConfig, Leader

from RobotFiles.WebRTC import RobotRTC

from pathlib import Path
import argparse
from getpass import getpass
import os
import time
import json
import threading

import asyncio
import logging

SERVER_URL = os.getenv("PUBLISH_URL", "https://primowang.com/").rstrip("/")

## Logger start
from logging.handlers import TimedRotatingFileHandler
WORKSPACE_DIR = Path(__file__).resolve().parent
log_path = WORKSPACE_DIR / "local_logs/robot.log"
log_path.parent.mkdir(parents=True, exist_ok=True)

handler = TimedRotatingFileHandler(
    filename=log_path,
    when="M",
    interval=1,        # New file every minute
    backupCount=179,   # 179 backups + current file ≈ 3 hours
    encoding="utf-8",
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[handler, logging.StreamHandler()],
    force=True,
)

class IceLogFilter(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        return not (
            "Check CandidatePair(" in message
            or "Could not bind to" in message
        )
logging.getLogger("aioice.ice").addFilter(IceLogFilter())
logger = logging.getLogger(__name__)
## Logger end

latency_log = [0.0] * 10
latency_log_lock = threading.Lock()
"""Global Variables End"""

def sync_states_to_follower(device, states: dict):
    # Convert motor states received over WebRTC into follower position targets.
    if states is None or "motors" not in states:
        return
    action = {f"{motor}.pos": data["position"] for motor, data in states["motors"].items()}
    device.send_action(action)

async def receive_loop(rtc, teleop_device):
    while True:
        control = await rtc.receive_control()
        control = json.loads(control)
        latency_ms = (time.time() - control["timestamp"]) * 1000
        with latency_log_lock:
            latency_log.pop(0)
            latency_log.append(latency_ms)
        if "leadState" in control:
            sync_states_to_follower(teleop_device, control["leadState"])
        logger.info("Received control: %r", control)


async def send_loop(rtc, teleop_device):
    while True:
        with latency_log_lock:
            latency_avg_ms = sum(latency_log) / len(latency_log)
        message = {
            "timestamp": time.time(),
            "latency_avg_ms": latency_avg_ms,
            "ctrl": teleop_device.get_position(),
        }
        send_result = rtc.send_control(message)
        logger.info("telemetry: %s", json.dumps({**message, "sent": send_result}))
        await asyncio.sleep(0.1)  # Send every 100 ms

async def run_follower(teleop_device, robot_name, robot_role, api_key):
    rtc = RobotRTC(
        SERVER_URL = SERVER_URL, 
        ROBOT_NAME = robot_name,
        ROBOT_ROLE = robot_role,
        API_KEY = api_key)

    await rtc.connect()
    print("connected")
    logger.info("RTC connected")
    try:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(receive_loop(rtc, teleop_device))
            tasks.create_task(send_loop(rtc, teleop_device))
    finally:
        await rtc.close()

async def run_leader(teleop_device, robot_name, robot_role, api_key):
    rtc = RobotRTC(
        SERVER_URL=SERVER_URL,
        ROBOT_NAME=robot_name,
        ROBOT_ROLE=robot_role,
        API_KEY=api_key)

    await rtc.connect()
    print("connected")
    logger.info("RTC connected")
    try:
        await send_loop(rtc, teleop_device)
    finally:
        await rtc.close()

def load_api_key():
    key_file = WORKSPACE_DIR / "APIKey.txt"
    api_key = key_file.read_text(encoding="utf-8").strip() if key_file.exists() else ""
    if not api_key:
        while not api_key:
            api_key = getpass("Enter the API key: ").strip()
        key_file.write_text(api_key + "\n", encoding="utf-8")
    return api_key


def main():
    ### Input
    parser = argparse.ArgumentParser(description="Connect a robot arm using automatic serial-port detection.")
    parser.add_argument("--robot-name", required=True, help="Robot name used for server routing")
    parser.add_argument("--robot-role", required=True, help="Robot role, e.g. Base")
    parser.add_argument("--type", choices=("Follower", "Leader"), required=True, help="Arm type")
    args = parser.parse_args()
    teleop_device = None
    try:
        api_key = load_api_key()
        if args.type == "Follower":
            teleop_config = FollowerRobotConfig(
                port="auto",
                id="PrimoFollower",
                calibration_dir=Path("RobotFiles/calibration/follower")
            )
            teleop_device = Follower(teleop_config)
            teleop_device.connect()
            asyncio.run(run_follower(teleop_device, args.robot_name, args.robot_role, api_key))

        elif args.type == "Leader":
            teleop_config = LeaderTeleopConfig(
                port="auto",
                id="PrimoLeader",
                calibration_dir=Path("RobotFiles/calibration/leader")
            )
            teleop_device = Leader(teleop_config)
            teleop_device.connect()
            asyncio.run(run_leader(teleop_device, args.robot_name, args.robot_role, api_key))
        
    except KeyboardInterrupt:
        print("\nStopping...")
    

    finally:
        if teleop_device is not None and teleop_device.is_connected:
            teleop_device.disconnect()
        print("Shutdown complete")
            

if __name__ == "__main__":
    main()
    
    
