

from RobotFiles.Drivers.LeaderArm import LeaderTeleopConfig, Leader
from RobotFiles.Drivers.FollowerArm import FollowerRobotConfig, Follower
from RobotFiles.WebRTC import RobotRTC, CameraSource

from WebRepo.server.plot_logger import log_and_plot, flush_plot, set_layout, set_role, set_realtime_plot, plot_json

from pathlib import Path
import argparse
import os
import time
import json
import threading

import websockets
import asyncio
import logging

from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer


"""Global Variables"""
motorId = ['shoulder_pan', 'shoulder_lift', 'elbow_flex', 'wrist_flex', 'wrist_roll', 'gripper']
teleop_device = None

SERVER_URL = "http://127.0.0.1:8000"
ROBOT_NAME = "CapstoneBot"
ROBOT_ROLE = "Base"
CAMERA_SOURCE = 0            # /dev/video0 (USB webcam)
CAMERA_BACKEND = "opencv"    # "picamera2" for the ribbon camera
API_KEY = "uapi_6eae160cc1.e7Tvl37uJmipwBuji6KHd5Bq4JQPhAK_Rb0GinoBcGs"

SERVER_URL = os.getenv("PUBLISH_URL", "https://primowang.com/").rstrip("/")
API_KEY = "uapi_a5e00f7b89.C-u6qZly-9-1wEBepQR26UyuOzSF8Uig3jw_xGfHPZU"

## Logger start
import logging
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


payload = None
payload_lock = threading.Lock()
payload_temp = None
payload_temp_lock = threading.Lock()
latency_log = [0.0] * 10
latency_log_lock = threading.Lock()
stop_event = threading.Event()
ExcutionPeriod = 0.01
"""Global Variables End"""

def get_all_states(device) -> dict[str, dict]:
    """Read position, velocity, and current for all motors.
    Returns: {motor_name: {"position": val, "velocity": val, "current": val}}
    """
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
        }
    }

def sync_states_to_follower(device, states: dict):
    #Convert a get_all_states() payload received over WebRTC into a RobotAction dict and send it to the follower arm.
    if states is None or "motors" not in states:
        return
    action = {f"{motor}.pos": data["position"] for motor, data in states["motors"].items()}
    device.send_action(action)

def run_leader_local(device):
    global payload, payload_lock
    while not stop_event.is_set():
        states = get_all_states(device)
        with payload_lock:
            payload = states
        time.sleep(ExcutionPeriod)

def run_follower_local(device):
    global payload, payload_lock
    while not stop_event.is_set():
        with payload_lock:
            states = payload
        print(states)
        sync_states_to_follower(device, states)
        time.sleep(ExcutionPeriod)

async def receive_loop(rtc):
    while True:
        control = await rtc.receive_control()
        control = json.loads(control)
        latency_ms = (time.time() - control["timestamp"]) * 1000
        with latency_log_lock:
            latency_log.pop(0)
            latency_log.append(latency_ms)
        logger.info("Received control: %r", control)


async def send_loop(rtc):
    while True:
        with latency_log_lock:
            latency_avg_ms = sum(latency_log) / len(latency_log)
        message = {"timestamp": time.time(), "latency_avg_ms": latency_avg_ms}
        send_result = rtc.send_control(message)
        logger.info("telemetry: %s", json.dumps({**message, "sent": send_result}))
        await asyncio.sleep(0.1)  # Send every 100 ms

async def run_follower():
    # The track only reads latest_frame(); we own the camera and close it after RTC.
    camera = CameraSource(source=CAMERA_SOURCE, backend=CAMERA_BACKEND).start()
    rtc = RobotRTC(
        video_source = camera,
        SERVER_URL = SERVER_URL,
        ROBOT_NAME = ROBOT_NAME,
        ROBOT_ROLE = ROBOT_ROLE,
        API_KEY = API_KEY)

    try:
        await rtc.connect()
        print("connected")
        logger.info("RTC connected")
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(receive_loop(rtc))
            tasks.create_task(send_loop(rtc))
    finally:
        await rtc.close()
        await camera.close()

def main():
    global ExcutionPeriod
    global teleop_device
    thread_leader = None
    thread_follower = None
    leader = None
    follower = None
    ### Input
    parser = argparse.ArgumentParser()
    parser.add_argument("--type", type=str, required=True, help="Mode type --type(e.g., Follower or Leader)")
    parser.add_argument("--COM", type=str, required=True, help="COM Port --COM(e.g COM2...)")
    parser.add_argument("--local", type=str, required=True, help="Local Mode --Local (e.g t/f...)")
    args = parser.parse_args()
    try:
        if args.local == "t":
                    leader_config = LeaderTeleopConfig(
                        port="COM3",
                        id="PrimoLeader",
                        calibration_dir=Path("RobotFiles/calibration/leader")
                        )
                    leader = Leader(leader_config)
                    leader.connect()
                    thread_leader = threading.Thread(
                        target = run_leader_local,
                        args=(leader,),
                    )
                    thread_leader.start()
        
        
                    follower_config = FollowerRobotConfig(
                        port="COM4",
                        id="PrimoFollower",
                        calibration_dir=Path("RobotFiles/calibration/follower")
                    )
                    follower = Follower(follower_config)
                    follower.connect()
                    thread_follower = threading.Thread(
                        target = run_follower_local,
                        args=(follower,),
                    )
                    thread_follower.start()
        
                    while thread_leader.is_alive() and thread_follower.is_alive():
                        thread_leader.join(timeout=0.2)
                        thread_follower.join(timeout=0.2)
                        
        elif args.type == "Follower":
            # teleop_config = FollowerRobotConfig(
            #     port=args.COM,
            #     id="PrimoFollower",
            #     calibration_dir=Path("RobotFiles/calibration/follower")
            # )
            # teleop_device = Follower(teleop_config)
            # teleop_device.connect()
            asyncio.run(run_follower())

        elif args.type == "Leader":
            # teleop_config = LeaderTeleopConfig(
            #     port=args.COM,
            #     id="PrimoLeader",
            #     calibration_dir=Path("RobotFiles/calibration/leader")
            #     )
            # teleop_device = Leader(teleop_config)
            # teleop_device.connect()
            print("nothing yet")
        
    except KeyboardInterrupt:
        print("\nStopping...")
    

    finally:
        # This lets both worker loops exit.
        stop_event.set()
        if thread_leader is not None:
            thread_leader.join(timeout=2.0)
        if thread_follower is not None:
            thread_follower.join(timeout=2.0)
        if leader is not None and leader.is_connected:
            leader.disconnect()
        if follower is not None and follower.is_connected:
            follower.disconnect()
        if teleop_device is not None and teleop_device.is_connected:
            teleop_device.disconnect()
        print("Shutdown complete")
            

if __name__ == "__main__":
    main()
    
    
