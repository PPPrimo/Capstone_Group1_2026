

from RobotFiles.Drivers.FollowerArm import FollowerRobotConfig, Follower
from RobotFiles.Drivers.LeaderArm import LeaderTeleopConfig, Leader
from RobotFiles.WebRTC import RobotRTC, CameraSource, SideBySide 

from RobotFiles.WebRTC import RobotRTC
from RobotFiles.CollisionGuard import CollisionGuard, LeaderLock

from pathlib import Path
import argparse
from getpass import getpass
import os
import time
import json
import threading
import uuid
from urllib.parse import quote

import asyncio
import logging
import aiohttp
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription

# Cameras streamed to the browser
CAMERAS = [
    {"source": 0, "backend": "opencv"},   # USB webcam (/dev/video0)
    {"backend": "picamera2"},             # Pi ribbon camera
]

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

ctrlLatency_log = [0.0] * 10  # (ms) Recent leader-to-follower latencies
ctrlLatency_log_lock = threading.Lock()
received_count = 0
STATUS_PERIOD = 5.0
stop_event = threading.Event()
k_readFreq = 10  # (Hz) rate the arm joints are read and sent (RL-4.1)
"""Global Variables End"""

def sync_states_to_follower(guard, leader_state: dict):
    # Hand leader joint angles received over WebRTC to the collision guard, which moves the follower (RF-3.2)
    if leader_state is None or "motors" not in leader_state:
        return
    m_leaderPos = {motor: data["position"] for motor, data in leader_state["motors"].items()}
    guard.set_target(m_leaderPos)

async def receive_loop(rtc, guard):
    global received_count
    while True:
        control = await rtc.receive_control()
        control = json.loads(control)
        ctrlLatency_ms = (time.time() - control["timestamp"]) * 1000
        with ctrlLatency_log_lock:
            ctrlLatency_log.pop(0)
            ctrlLatency_log.append(ctrlLatency_ms)
            received_count += 1
        if "leadState" in control:
            sync_states_to_follower(guard, control["leadState"])
        logger.info("Received control: %r", control)


async def send_loop(send_control, teleop_device, guard=None):
    global received_count
    sent_count = 0
    last_status = time.monotonic()
    while True:
        with ctrlLatency_log_lock:
            ctrlLatency_avg_ms = sum(ctrlLatency_log) / len(ctrlLatency_log)
        # With a guard, reuse its latest read instead of a second bus read
        m_armPos = None
        if guard is not None:
            m_armPos = guard.positions()
        if m_armPos is None:
            m_armPos = teleop_device.get_position()
        message = {
            "timestamp": time.time(),
            "latency_avg_ms": ctrlLatency_avg_ms,
            "leadState": {
                "motors": {
                    motor: {"position": position}
                    for motor, position in m_armPos.items()
                }
            },
        }
        if guard is not None:
            message["guard"] = guard.status()  # repeated every send, so a lost packet doesn't hide a stop
        send_result = send_control(message)
        sent_count += bool(send_result)
        logger.debug("telemetry: %s", json.dumps({**message, "sent": send_result}))

        if time.monotonic() - last_status >= STATUS_PERIOD:
            with ctrlLatency_log_lock:
                received, received_count = received_count, 0
            logger.info("status: sent %d telemetry, received %d controls, latency avg %.1f ms",
                        sent_count, received, ctrlLatency_avg_ms)
            sent_count = 0
            last_status = time.monotonic()        
        await asyncio.sleep(1 / k_readFreq)

async def run_follower(teleop_device, guard, robot_name, robot_role, api_key):
    # The track only reads latest_frame(); we own the cameras and close them after RTC.
    cameras = [CameraSource(**camera).start() for camera in CAMERAS]
    rtc = RobotRTC(
        video_source = cameras[0] if len(cameras) == 1 else SideBySide(*cameras),
        SERVER_URL = SERVER_URL, 
        ROBOT_NAME = robot_name,
        ROBOT_ROLE = robot_role,
        API_KEY = api_key)

    try:
        await rtc.connect()
        print("connected")
        logger.info("RTC connected")
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(receive_loop(rtc, guard))
            tasks.create_task(send_loop(rtc.send_control, teleop_device, guard))
    finally:
        await rtc.close()
        for camera in cameras:
            await camera.close()
        for camera in cameras:
            await camera.close()

k_guardStatusTimeout = 1.0  # (s) without robot telemetry before the leader unlocks; the robot still holds its stop

async def lock_loop(teleop_device, lock, latest):
    # Mirror the robot's collision stop on this leader, using the guard status from its telemetry
    while True:
        # Use the robot's last guard status, unless it is too old (then act as if not stopped).
        if time.monotonic() - latest["time"] <= k_guardStatusTimeout:
            status = latest["guard"]
        else:
            status = {}
        try:
            lock.update(status, teleop_device.get_position(), teleop_device.get_current())
        except Exception as error:  # a bus hiccup must not stop teleop
            logger.warning("Leader lock update failed: %s", error)
        await asyncio.sleep(0.02)

async def run_leader(teleop_device, lock, robot_name, robot_role, api_key):
    # Act as the direct client; name/role identify the follower to control
    base = SERVER_URL.replace("wss://", "https://", 1).replace("ws://", "http://", 1)
    path = f"/api/webrtc/client/offer/{quote(robot_name, safe='')}/{quote(robot_role, safe='')}"
    delay = 0.5
    while True:
        pc = None
        locker = None
        try:
            async with aiohttp.ClientSession(headers={"X-API-Key": api_key}, timeout=aiohttp.ClientTimeout(total=40)) as http:
                async with http.get(f"{base}/api/webrtc/ice-servers") as response:
                    if response.status in (401, 403):
                        raise PermissionError("API key was rejected")
                    response.raise_for_status()
                    settings = await response.json()
                pc = RTCPeerConnection(RTCConfiguration(iceServers=[RTCIceServer(**item) for item in settings["iceServers"]]))
                channel = pc.createDataChannel("teleop", ordered=False, maxRetransmits=0)
                ready = asyncio.Event()
                channel.on("open", ready.set)
                latest = {"guard": {}, "time": 0.0}

                @channel.on("message")
                def on_message(message):
                    # Robot telemetry carries the collision guard's status; keep the newest one
                    try:
                        guard_status = json.loads(message).get("guard")
                    except (ValueError, AttributeError):
                        return
                    if isinstance(guard_status, dict):
                        latest["guard"] = guard_status
                        latest["time"] = time.monotonic()
                await pc.setLocalDescription(await pc.createOffer())
                async with http.ws_connect(f"{base}{path}", params={"api_key": api_key}) as ws:
                    await ws.send_json({
                        "type": "offer", "sdp": pc.localDescription.sdp,
                        "request_id": uuid.uuid4().hex, "direct_control": True,
                    })
                    answer = await ws.receive_json(timeout=40)
                    if answer["type"] == "error":
                        if not answer["retryable"]:
                            raise ValueError(answer["error"])
                        raise ConnectionError(answer["error"])
                    await pc.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type=answer["type"]))
                async with asyncio.timeout(30):
                    await ready.wait()
                delay = 0.5
                logger.info("Direct RTC connected to %s/%s", robot_name, robot_role)

                def send_direct(message):
                    if channel.readyState != "open":
                        raise ConnectionError("Direct control channel closed")
                    channel.send(json.dumps(message))
                    return True

                locker = asyncio.create_task(lock_loop(teleop_device, lock, latest))
                await send_loop(send_direct, teleop_device)
        except (aiohttp.ClientError, ConnectionError, TimeoutError) as error:
            logger.warning("Direct RTC disconnected: %s; retrying", error)
        finally:
            if locker is not None:
                locker.cancel()
                await asyncio.gather(locker, return_exceptions=True)
                lock.release()  # no link to the robot: leave the leader free
            if pc is not None:
                await pc.close()
        await asyncio.sleep(delay)
        delay = min(delay * 2, 4)

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
    parser.add_argument("--port", default="auto", help="Serial port of the arm, e.g. COM6 or /dev/ttyACM0 (default: auto-detect)")
    parser.add_argument("--guard-log", action="store_true", help="Follower: write a collision-guard CSV to local_logs/")
    parser.add_argument("--lock-torque", type=float, default=50, help="Leader: holding strength while the robot is stopped, %% of max (default 50)")
    args = parser.parse_args()
    teleop_device = None
    guard = None
    lock = None
    try:
        api_key = load_api_key()
        if args.type == "Follower":
            teleop_config = FollowerRobotConfig(
                port=args.port,
                id="PrimoFollower",
                calibration_dir=Path("RobotFiles/calibration/follower")
            )
            teleop_device = Follower(teleop_config)
            teleop_device.connect()
            if args.guard_log:
                guard_log_dir = WORKSPACE_DIR / "local_logs"
            else:
                guard_log_dir = None
            guard = CollisionGuard(teleop_device, log_dir=guard_log_dir).start()
            asyncio.run(run_follower(teleop_device, guard, args.robot_name, args.robot_role, api_key))

        elif args.type == "Leader":
            teleop_config = LeaderTeleopConfig(
                port=args.port,
                id="PrimoLeader",
                calibration_dir=Path("RobotFiles/calibration/leader")
            )
            teleop_device = Leader(teleop_config)
            teleop_device.connect()
            lock = LeaderLock(teleop_device, torque_pct=args.lock_torque).start()
            asyncio.run(run_leader(teleop_device, lock, args.robot_name, args.robot_role, api_key))
        
    except KeyboardInterrupt:
        print("\nStopping...")
    

    finally:
        if guard is not None:
            guard.stop()  # before disconnect, so nothing writes to a closed bus
        if lock is not None:
            try:
                lock.close()  # leader limp again, torque limits restored
            except Exception as error:
                print(f"Leader lock cleanup failed: {error}")
        if teleop_device is not None and teleop_device.is_connected:
            teleop_device.disconnect()
        print("Shutdown complete")
            

if __name__ == "__main__":
    main()
    
    
