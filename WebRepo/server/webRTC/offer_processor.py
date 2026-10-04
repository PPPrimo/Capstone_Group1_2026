import asyncio

from aiortc import RTCSessionDescription
from aiortc.sdp import SessionDescription


class RTCOfferProcessor:
    def __init__(self, webRTC_manager):
        self.webrtc_manager = webRTC_manager

    #validate offer is legit on role, content, size, 
    def validate_offer(self, peer_role: str, offer: dict):
        if peer_role not in ("MobileRobot", "ServerClient"):
            raise ValueError(f"Unknown WebRTC peer role: {peer_role}")
        if not isinstance(offer, dict) or offer.get("type") != "offer" or not isinstance(offer.get("sdp"), str):
            raise ValueError("Expected an SDP offer with type='offer'")
        direct = offer.get("direct_control", False)
        if type(direct) is not bool or (direct and peer_role != "ServerClient"):
            raise ValueError("Invalid direct_control mode")
        limit = 48000 if direct else 1000000
        if len(offer["sdp"].encode("utf-8")) > limit or not offer["sdp"].startswith("v=0"):
            raise ValueError("Invalid or oversized SDP")
        parsed = SessionDescription.parse(offer["sdp"])
        if not parsed.media:
            raise ValueError("Offer contains no media or data channel")
        if direct and not any(m.kind == "application" and m.port != 0 for m in parsed.media):
            raise ValueError("Direct control requires a data channel")
        if peer_role == "ServerClient" and not direct and not any(m.kind == "video" and m.port != 0 and m.direction in (None, "recvonly", "sendrecv") for m in parsed.media):
            raise ValueError("Client must offer to receive robot video")
        return parsed

    async def process(self, peer_role: str, robot_id: str, robot_role: str, offer: dict): #reviewed (client use case does not exist)
        self.validate_offer(peer_role, offer)
        manager = self.webrtc_manager.get_robot_manager(robot_id)
        if manager is None or not manager.owns_lease():
            raise RuntimeError("This worker no longer owns the robot")
        lock = manager.offer_locks.setdefault(robot_role, asyncio.Lock())
        async with asyncio.timeout(self.webrtc_manager.offer_timeout):
            async with lock:
                if not await self.webrtc_manager.is_owner(manager):
                    raise RuntimeError("Robot ownership changed")
                if peer_role == "MobileRobot":
                    return await self._process_robot(robot_id, robot_role, offer)
                return await self._process_client(robot_id, robot_role, offer)

    async def _process_robot(self, robot_id: str, robot_role: str, offer: dict): #reviewed - basically unchanged
        manager = self.webrtc_manager.get_robot_manager(robot_id)
        # Each new request is a new connection; retries are deduplicated before process().
        robot = await manager.create_robot_session(robot_role)
        try:
            await robot.pc.setRemoteDescription(RTCSessionDescription(sdp=offer["sdp"], type=offer["type"]))
            answer = await robot.pc.createAnswer()
            await robot.pc.setLocalDescription(answer)
            if robot._closing or manager.get_robot_session(robot_role) is not robot or not await self.webrtc_manager.is_owner(manager):
                raise RuntimeError("Robot was replaced or ownership changed during negotiation")
            robot.start_task("ready", self.webrtc_manager.watch_robot_ready(manager, robot))
            robot.start_task("connect_timeout", self._wait_connected(robot))
            return robot, self._answer(robot.pc)
        except BaseException:
            await robot.close()
            raise

    async def _process_client(self, robot_id: str, robot_role: str, offer: dict):#use case does not exist
        manager = self.webrtc_manager.get_robot_manager(robot_id)
        robot = manager.get_robot_session(robot_role)
        if robot is None or robot._closing:
            raise RuntimeError(f"Robot role {robot_role!r} is offline")
        # The track is announced during robot SDP negotiation; never block this role on first-frame arrival.
        if robot.video_track is None or robot.video_track.readyState != "live":
            raise RuntimeError(f"Video is unavailable for {robot_id}:{robot_role}")
        client = await manager.create_client_session(robot_role)
        try:
            await client.pc.setRemoteDescription(RTCSessionDescription(sdp=offer["sdp"], type=offer["type"]))
            if not client.attach_robot_video():
                raise RuntimeError(f"Video is unavailable for {robot_id}:{robot_role}")
            answer = await client.pc.createAnswer()
            await client.pc.setLocalDescription(answer)
            if client._closing or manager.get_client_session(robot_role) is not client or not await self.webrtc_manager.is_owner(manager):
                raise RuntimeError("Client was replaced or ownership changed during negotiation")
            client.start_task("connect_timeout", self._wait_connected(client))
            return client, self._answer(client.pc)
        except BaseException:
            await client.close()
            raise

    async def _wait_connected(self, session): #reviewed
        try:
            async with asyncio.timeout(self.webrtc_manager.connect_timeout):
                while not session.connected and not session._closing:
                    await asyncio.sleep(0.1)
        except TimeoutError:
            await session.close()

    def _answer(self, pc): #reviewed
        return {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}
