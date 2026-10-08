

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
API_KEY = "uapi_01bce6e6b5.ukkEBSUDqVkwC_bF7Pa8OXR_ju1rEj6aq1TDAdvu1I0"

# SERVER_URL = os.getenv("PUBLISH_URL", "https://primowang.com/").rstrip("/")
# API_KEY = "uapi_1a5baab4e2.dhNJpYgzzNjNa9uxXBo2rTZOMhEY6TEti8bekJ0QKkA"

logging.basicConfig(
    level=logging.INFO, 
    format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("capstone.log", encoding="utf-8"),
    ],
    force=True)
logger = logging.getLogger(__name__)

payload = None
payload_lock = threading.Lock()
payload_temp = None
payload_temp_lock = threading.Lock()
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

async def run_follower():
    rtc = RobotRTC(
        SERVER_URL = SERVER_URL, 
        ROBOT_NAME = ROBOT_NAME, 
        ROBOT_ROLE = ROBOT_ROLE, 
        API_KEY = API_KEY)

    await rtc.connect()
    print("connected")
    logger.info("RTC connected")

    while True:
        control = await rtc.receive_control()
        logger.info("Received control: %r", control)

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
    
    
