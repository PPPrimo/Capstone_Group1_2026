import asyncio
import base64
import json
import inspect
import logging
import time
import uuid
from urllib.parse import quote, urlsplit

import aiohttp
from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription
from aiortc.exceptions import InvalidStateError

from .RtcVideo import SwitchableVideoTrack

logger = logging.getLogger(__name__)
OFFER_TIMEOUT = 20
SIGNAL_TIMEOUT = 40
CONNECT_TIMEOUT = 30
MAX_COMMAND_BYTES = 16384
MAX_BUFFERED_BYTES = 65536


class AuthenticationError(RuntimeError):
    pass


class RobotRTC:
    def __init__(self, video_source=None, *, SERVER_URL, ROBOT_NAME, ROBOT_ROLE, API_KEY):
        self.pc = None
        self.control_channel = None
        self.auxiliary_channel = None
        self._direct = None
        self._finished_negotiations = {}
        self._direct_task = None
        self.server_override = False
        self.server_command = None
        self.user_command = None
        self.command_timeout = 0.5
        self._last_command = None
        self.video_track = None
        self.video_source = video_source
        self.connected = False
        self.last_error = None
        self._runner = None
        self._closing = False
        self._close_task = None
        self._changed = asyncio.Event()
        self._transport_changed = asyncio.Event() #trigger the while loop to update connection state
        self._received = asyncio.Event() 
        self._receive_task = None
        self._receiving = False
        self._manual_receiver = None
        self.SERVER_URL = SERVER_URL
        self.ROBOT_NAME = ROBOT_NAME
        self.ROBOT_ROLE = ROBOT_ROLE
        self.API_KEY = API_KEY

    def set_video_source(self, source):
        """Attach an already-started source, or None for blank video. Caller owns it."""
        if source is not None and not callable(getattr(source, "latest_frame", None)):
            raise TypeError("Video source must provide nonblocking latest_frame()")
        self.video_source = source
        if self.video_track is not None:
            self.video_track.source = source
        if self._direct is not None and self._direct.get("track") is not None:
            self._direct["track"].source = source

    async def connect(self, timeout=90): #reviewed
        #safety handler for connect
        if self._closing:
            raise RuntimeError("RTC is closed; create a new RobotRTC instance")
        if not self.API_KEY or self.API_KEY == "your_api_key":
            raise ValueError("Set API_KEY in rtc_connection.py")
        if not self.ROBOT_NAME or not self.ROBOT_ROLE:
            raise ValueError("Set ROBOT_NAME and ROBOT_ROLE")
        parts = urlsplit(self.SERVER_URL)
        if parts.scheme not in ("http", "https", "ws", "wss") or not parts.netloc or parts.query or parts.fragment or parts.username:
            raise ValueError("SERVER_URL must be an HTTP(S) or WS(S) base URL without credentials/query/fragment")
        if self._runner is None:
            self._runner = asyncio.create_task(self._run(), name="robot-rtc")
        try:
            async with asyncio.timeout(timeout):
                while not self.connected:
                    self._changed.clear()
                    if self._runner.done():
                        raise self.last_error or RuntimeError("RTC connection stopped")
                    if self._closing:
                        raise RuntimeError("RTC closed while connecting")
                    await self._changed.wait()
        except BaseException:
            await self.close()
            raise
        return self

    def send_control(self, message):
        """Send telemetry to the server and the connected direct client, when present."""
        if not isinstance(message, (str, bytes)):
            message = json.dumps(message, separators=(",", ":"), allow_nan=False)
        size = len(message.encode("utf-8")) if isinstance(message, str) else len(message)
        if size > MAX_COMMAND_BYTES:
            raise ValueError("Control message exceeds MAX_COMMAND_BYTES")
        if self._closing:
            return False
        channels = [self.control_channel] if self.connected else []
        if self._direct is not None and self._direct.get("established"):
            channels.append(self._direct.get("channel"))
        sent = False
        for channel in channels:
            if channel is None or channel.readyState != "open" or channel.bufferedAmount + size > MAX_BUFFERED_BYTES:
                continue
            try:
                channel.send(message)
                sent = True
            except InvalidStateError:
                continue
        return sent

    async def _next_control(self, timeout=None): #reviewed
        #chooses which command to follow (server overide or user)
        async with asyncio.timeout(timeout):
            while True:
                self._received.clear()
                if self._closing or (self._runner is not None and self._runner.done()):
                    raise self.last_error or ConnectionError("RTC is closed")
                selected = self.server_command if self.server_override else self.user_command
                if selected is not None and selected is not self._last_command and time.monotonic() - selected[0] <= self.command_timeout:
                    self._last_command = selected
                    return selected[1]
                await self._received.wait()

    async def receive_control(self, timeout=None): #reviewed
        #returns the latest recieved command (this is meant for occasional access, not for periodic use)
        if self._receive_task is not None and not self._receive_task.done():
            raise RuntimeError("Stop callback receiving before calling receive_control()")
        if self._manual_receiver is not None:
            raise RuntimeError("Only one receive_control() consumer is allowed")
        self._manual_receiver = asyncio.current_task()
        try:
            return await self._next_control(timeout)
        finally:
            self._manual_receiver = None

    def start_receiving(self, callback): #reviewed
        #recieves messages and excute the function ti was given
        if not callable(callback):
            raise TypeError("Receive callback must be callable")
        if self._closing or (self._runner is not None and self._runner.done()):
            raise RuntimeError("RTC is closed")
        if self._receive_task is not None and not self._receive_task.done():
            raise RuntimeError("Callback receiving is already active or stopping")
        if self._manual_receiver is not None:
            raise RuntimeError("A receive_control() call is already waiting")
        loop = asyncio.get_running_loop()
        self.server_command = self.user_command = self._last_command = None
        self._receiving = True
        self._receive_task = loop.create_task(self._dispatch_control(callback), name="robot-rtc-receiver")

    async def stop_receiving(self):
        #stops the recieving callback
        self._receiving = False
        self.server_command = self.user_command = self._last_command = None
        task = self._receive_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if self._receive_task is task:
                self._receive_task = None

    async def _dispatch_control(self, callback):
        #excutes the callback function
        try:
            while self._receiving and not self._closing:
                try:
                    message = await self._next_control()
                except ConnectionError as exc:
                    logger.error("Closed or permanently failed connection. | %s", str(exc))
                    return
                if not self._receiving or self._closing:
                    return
                result = callback(message)
                if inspect.isawaitable(result):
                    await result
        finally:
            if self._receive_task is asyncio.current_task():
                self._receiving = False
                self._receive_task = None

    def _setup_handlers(self, pc):
        @pc.on("connectionstatechange")
        def on_state():
            if self._is_current(pc):
                if pc.connectionState != "connected":
                    self.connected = False
                    self.server_command = self.user_command = self._last_command = None
                self._transport_changed.set()

        @pc.on("datachannel")
        def on_datachannel(channel):
            #avoids old reference, self cleaning in progress, or wrong channel
            if not self._is_current(pc):
                channel.close()
                return
            if channel.label not in ("teleop", "auxiliary"):
                raise ValueError(f"Unexpected channel: {channel.label}")
            attribute = "control_channel" if channel.label == "teleop" else "auxiliary_channel"
            setattr(self, attribute, channel)
            self._transport_changed.set()
            
            @channel.on("open")
            def on_open():
                self._transport_changed.set()

            @channel.on("close")
            def on_close():
                if self._is_current(pc):
                    self.connected = False
                    self.server_command = self.user_command = self._last_command = None
                    self._transport_changed.set()

            @channel.on("message")
            def on_message(message):
                if not self._is_current(pc) or pc.connectionState != "connected":
                    return
                if channel.label == "auxiliary":
                    self._receive_auxiliary(message)
                    return
                envelope = json.loads(message)
                source, command = envelope["source"], envelope["command"]
                if envelope["identifier"] != "CMD" or source not in ("server", "user"):
                    raise ValueError("Invalid command or sender")
                if envelope.get("binary") is True:
                    command = base64.b64decode(command, validate=True)
                if source == "server" or self._direct is None:
                    self._store_command(command, source)

    async def _negotiate(self, pc): #reviewed
        # Creating a bootstrap channel puts SCTP in the offer. The server creates teleop.
        pc.createDataChannel("_negotiation", ordered=False, maxRetransmits=0)
        async with asyncio.timeout(OFFER_TIMEOUT):
            await pc.setLocalDescription(await pc.createOffer())
        if pc.iceGatheringState != "complete":
            raise ConnectionError("ICE gathering did not complete")
        request_id = uuid.uuid4().hex
        base = self.SERVER_URL.rstrip("/")
        url = f"{base}/api/webrtc/robot/offer/{quote(self.ROBOT_NAME, safe='')}/{quote(self.ROBOT_ROLE, safe='')}"
        # API key is a query parameter because that is what the supplied server reads.
        # Never log this URL or raw aiohttp exceptions (they can include the API key).
        try:
            async with asyncio.timeout(SIGNAL_TIMEOUT):
                async with aiohttp.ClientSession() as http:
                    async with http.ws_connect(url, params={"api_key": self.API_KEY}, max_msg_size=1000000) as ws:
                        await ws.send_json({"type": "offer", "sdp": pc.localDescription.sdp, "request_id": request_id})
                        message = await ws.receive()
                        if message.type != aiohttp.WSMsgType.TEXT:
                            if ws.close_code in (4401, 4403):
                                raise AuthenticationError("RTC authentication rejected")
                            raise ConnectionError("Signaling closed before the SDP answer")
                        answer = json.loads(message.data)
                        if not isinstance(answer, dict):
                            raise RuntimeError("Invalid signaling response")
                        if answer.get("type") == "error":
                            if answer.get("retryable") is True:
                                raise ConnectionError("RTC service is not ready")
                            raise ValueError("Server rejected the RTC offer")
                        if answer.get("type") != "answer" or not isinstance(answer.get("sdp"), str):
                            raise RuntimeError("Invalid SDP answer")
                        if answer.get("request_id", request_id) != request_id:
                            raise RuntimeError("Answer request_id mismatch")
                        await pc.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type="answer"))
            # Signaling is intentionally finished here; the peer connection stays alive.
        except aiohttp.WSServerHandshakeError as exc:
            if exc.status in (401, 403):
                raise AuthenticationError("RTC authentication rejected; check API_KEY and server access") from None
            raise ConnectionError(f"Signaling handshake failed (HTTP {exc.status})") from None
        except aiohttp.ClientError:
            raise ConnectionError("Cannot reach RTC signaling server") from None

    async def _fetch_ice_servers(self):
        parts = urlsplit(self.SERVER_URL)
        scheme = {"ws": "http", "wss": "https"}.get(parts.scheme, parts.scheme)
        base = parts._replace(scheme=scheme).geturl().rstrip("/")
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as http:
                async with http.get(
                    f"{base}/api/webrtc/ice-servers",
                    headers={"X-API-Key": self.API_KEY},
                    allow_redirects=False,
                ) as response:
                    if response.status in (401, 403):
                        raise AuthenticationError("RTC authentication rejected; check API_KEY")
                    if response.status != 200:
                        raise ConnectionError(f"Cannot fetch ICE settings (HTTP {response.status})")
                    settings = await response.json()
            servers = settings.get("iceServers") if isinstance(settings, dict) else None
            if not isinstance(servers, list) or not servers:
                raise ValueError("Invalid ICE settings")
            return [RTCIceServer(**item) for item in servers]
        except (aiohttp.ClientError, TimeoutError):
            raise ConnectionError("Cannot fetch valid RTC connection settings") from None

    async def _connection(self):
        #attempt 1 connection, check status upon statechange or 1 second, terminates after timeout
        ice_servers = await self._fetch_ice_servers()
        if self._closing:
            return
        pc = self.pc = RTCPeerConnection(RTCConfiguration(iceServers=ice_servers))
        track = self.video_track = SwitchableVideoTrack(self.video_source)
        try:
            pc.addTransceiver(track, direction="sendonly")
            self._setup_handlers(pc)
            await self._negotiate(pc)
            started = time.monotonic()
            disconnected_at = None
            ever_connected = False
            while not self._closing:
                self._transport_changed.clear()
                channel = self.control_channel
                auxiliary = self.auxiliary_channel
                state = pc.connectionState
                if state in ("failed", "closed") or any(ch is not None and ch.readyState == "closed" for ch in (channel, auxiliary)):
                    raise ConnectionError("RTC transport or control channel closed")
                ready = state == "connected" and all(ch is not None and ch.readyState == "open" for ch in (channel, auxiliary))
                was_connected = self.connected
                self.connected = ready
                if ready and not was_connected:
                    self._changed.set()
                if ready:
                    if not ever_connected:
                        logger.info("RTC connected: robot=%s role=%s", self.ROBOT_NAME, self.ROBOT_ROLE)
                    ever_connected = True
                    disconnected_at = None
                    self.last_error = None
                elif ever_connected:
                    disconnected_at = disconnected_at or time.monotonic()
                    if time.monotonic() - disconnected_at >= 5:
                        raise ConnectionError("RTC recovery timed out")
                elif time.monotonic() - started >= CONNECT_TIMEOUT:
                    raise TimeoutError("RTC/control channel did not connect")

                try:
                    await asyncio.wait_for(self._transport_changed.wait(), 1)
                except TimeoutError:
                    pass
        finally:
            self.connected = False
            await self._stop_direct()
            self.auxiliary_channel = None
            self._finished_negotiations.clear()
            self.server_override = False
            self.server_command = self.user_command = self._last_command = None
            self.pc = None
            self.control_channel = None
            self.video_track = None
            track.stop()
            await pc.close()
            self._changed.set()

    def _store_command(self, message, source):
        if not isinstance(message, (str, bytes)):
            message = json.dumps(message, separators=(",", ":"), allow_nan=False)
        value = (time.monotonic(), message)
        if source == "server":
            self.server_command = value
        else:
            self.user_command = value
        self._received.set()

    def _send_auxiliary(self, message): #reviewed
        channel = self.auxiliary_channel
        if not self._is_current(self.pc) or self.pc.connectionState != "connected" or channel is None or channel.readyState != "open":
            return False
        payload = json.dumps(message, separators=(",", ":"), allow_nan=False)
        size = len(payload.encode("utf-8"))
        if size > 65536:
            raise ValueError("Auxiliary message exceeds 64 KiB")
        if channel.bufferedAmount + size > 262144:
            return False
        try:
            channel.send(payload)
            return True
        except InvalidStateError:
            return False

    def _direct_message(self, state, identifier, **fields): #reviewed
        return {
            "ident": identifier, 
            "ngoID": state["id"], 
            "sessID": state["session_id"], 
            **fields}

    def _is_current(self, pc):
        return not self._closing and pc is not None and self.pc is pc

    def _direct_active(self, state):
        return self._direct is state and self.connected and not state["cancelled"]

    def _receive_auxiliary(self, payload):
        message = json.loads(payload)
        identifier = message["ident"]
        override = message["server_override"]
        if override != self.server_override:
            self.server_override = override
            self.server_command = self.user_command = self._last_command = None
            self._received.set()
        if identifier == "S_ovr":
            return
        key = (message["sessID"], message["ngoID"])
        if key in self._finished_negotiations:
            return  # Retransmission after completion, before the server saw confirmation.
        state = self._direct
        if identifier == "R_Off":
            if state is not None and (state["session_id"], state["id"]) == key:
                if not state["established"]:
                    self._send_auxiliary(self._direct_message(state, "R_Rdy", ready=True))
                    if "answer" in state:
                        self._send_auxiliary(self._direct_message(state, "R_Ans", answer=state["answer"]))
                return
            if state is not None:
                state["cancelled"] = True
                state["changed"].set()
                self._finished_negotiations[(state["session_id"], state["id"])] = time.monotonic()
                self._direct_task.cancel()
            state = {"id": key[1], "session_id": key[0], "offer": message["offer"], "changed": asyncio.Event(),
                     "answer_ack": False, "status_ack": None, "cancelled": False, "established": False,
                     "pc": None, "channel": None, "track": None}
            self._direct = state
            self.user_command = None
            self._send_auxiliary(self._direct_message(state, "R_Rdy", ready=True))
            self._direct_task = asyncio.create_task(self._run_direct(state), name=f"direct:{key[1]}")
            return
        if state is None or (state["session_id"], state["id"]) != key:
            return  # Acknowledgement for a replaced negotiation.
        if identifier == "R_Ack":
            state["answer_ack"] = True
        elif identifier == "R_Cnf":
            state["status_ack"] = message["clntCtrl"]
        elif identifier == "R_Cnl":
            state["cancelled"] = True
        else:
            raise ValueError(f"Unknown server message: {identifier}")
        state["changed"].set()

    def _setup_direct_handlers(self, state): #reviewed
        pc = state["pc"]

        @pc.on("connectionstatechange")
        def on_state(): #new
            if self._direct is state and pc.connectionState != "connected":
                self.user_command = None
            state["changed"].set()

        @pc.on("datachannel")
        def on_channel(channel): #new
            if self._direct is not state or channel.label != "teleop" or state["channel"] is not None:
                channel.close()
                return
            state["channel"] = channel
            channel.on("open", state["changed"].set)
            channel.on("close", state["changed"].set)

            @channel.on("message")
            def on_message(message): #new
                if self._direct_active(state) and state["established"] and pc.connectionState == "connected":
                    self._store_command(message, "user") #direct channel recieves command from user only
            state["changed"].set()

    async def _run_direct(self, state): #reviewed
        pc = None
        negotiating = None
        try:
            ice_servers = await self._fetch_ice_servers()
            if not self._direct_active(state):
                return
            pc = state["pc"] = RTCPeerConnection(RTCConfiguration(iceServers=ice_servers))
            self._setup_direct_handlers(state)
            negotiating = asyncio.create_task(self._answer_direct(state))
            async with asyncio.timeout(OFFER_TIMEOUT):
                while not negotiating.done(): #repeat informs server R_Rdy untill the negotiation is done
                    state["changed"].clear()
                    if not self._direct_active(state):
                        raise ConnectionError("Direct negotiation cancelled")
                    self._send_auxiliary(self._direct_message(state, "R_Rdy", ready=True))
                    await asyncio.wait({negotiating}, timeout=0.5)
                state["answer"] = await negotiating
            deadline = time.monotonic() + CONNECT_TIMEOUT
            disconnected_at = None
            while True:
                state["changed"].clear()
                if not self._direct_active(state):
                    break
                channel = state["channel"]
                if pc.connectionState in ("failed", "closed") or (channel is not None and channel.readyState == "closed"):
                    break
                ready = pc.connectionState == "connected" and channel is not None and channel.readyState == "open" and state["video_negotiated"]
                if ready:
                    disconnected_at = None
                    state["established"] = True
                    if state["status_ack"] != "T": #server has not confirmed full connection
                        self._send_auxiliary(self._direct_message(state, "R_Sts", clntCtrl="T"))
                elif state["established"]:
                    disconnected_at = disconnected_at or time.monotonic()
                    if time.monotonic() - disconnected_at >= 5:
                        break
                elif time.monotonic() >= deadline:
                    raise TimeoutError("Direct client connection timed out")
                if not state["answer_ack"] and not state["established"]: #server has not yet informs answer acknowledge
                    self._send_auxiliary(self._direct_message(state, "R_Ans", answer=state["answer"]))
                try:
                    await asyncio.wait_for(state["changed"].wait(), 0.5)
                except TimeoutError:
                    pass
        except (ConnectionError, TimeoutError) as exc:
            self._send_auxiliary(self._direct_message(state, "R_Err", error=str(exc) or type(exc).__name__))
        finally:
            if negotiating is not None:
                if not negotiating.done():
                    negotiating.cancel()
                await asyncio.gather(negotiating, return_exceptions=True)
            if pc is not None:
                await pc.close()
            if state["track"] is not None:
                state["track"].stop()
            if self._direct is state:
                self.user_command = None
            # Announce release until acknowledged; reliable SCTP also preserves ordering.
            if not self._closing and self.connected and not state["cancelled"]:
                for _ in range(6):
                    if state["status_ack"] == "F":
                        break
                    state["changed"].clear()
                    self._send_auxiliary(self._direct_message(state, "R_Sts", clntCtrl="F"))
                    try:
                        await asyncio.wait_for(state["changed"].wait(), 0.5)
                    except TimeoutError:
                        pass
            self._finished_negotiations[(state["session_id"], state["id"])] = time.monotonic()
            while len(self._finished_negotiations) > 256:
                self._finished_negotiations.pop(next(iter(self._finished_negotiations)))
            if self._direct is state:
                self._direct = None
            if self._direct_task is asyncio.current_task():
                self._direct_task = None

    async def _answer_direct(self, state): #reviewed
        #processes the offer
        pc = state["pc"]
        await pc.setRemoteDescription(RTCSessionDescription(sdp=state["offer"]["sdp"], type="offer"))
        from aiortc.sdp import SessionDescription
        parsed = SessionDescription.parse(state["offer"]["sdp"])
        #checks if media exists, media not rejected, user able to recieve media
        wants_video = any(m.kind == "video" and m.port != 0 and m.direction in (None, "recvonly", "sendrecv") for m in parsed.media)
        if wants_video:
            track = state["track"] = SwitchableVideoTrack(self.video_source)
            # One source per connection; the server keeps its independent track.
            pc.addTrack(track)
        await pc.setLocalDescription(await pc.createAnswer())
        state["video_negotiated"] = not wants_video or any(t.kind == "video" and t.currentDirection in ("sendonly", "sendrecv") and t.sender.track is not None for t in pc.getTransceivers())
        if not state["video_negotiated"]:
            raise RuntimeError("Direct video was not negotiated")
        answer = {"type": "answer", "sdp": pc.localDescription.sdp}
        if len(answer["sdp"].encode("utf-8")) > 48000:
            raise ValueError("Direct answer exceeds SDP size limit")
        return answer

    async def _stop_direct(self): #new
        state = self._direct
        task = self._direct_task
        if state is not None:
            state["cancelled"] = True
            state["changed"].set()
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._direct = None
        self._direct_task = None
        self.user_command = None

    async def _run(self): #reviewed
        #continously retry connection untill fatal error (timeout error not included)
        delay = 0.5
        try:
            while not self._closing:
                started = time.monotonic()
                try:
                    await self._connection()
                except AuthenticationError as exc:
                    self.last_error = exc
                    logger.error("%s", exc)
                    return
                except (ConnectionError, TimeoutError) as exc:
                    self.last_error = exc
                    logger.warning("%s; reconnecting", self.last_error)
                if time.monotonic() - started > 60:
                    delay = 0.5
                if not self._closing:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 4)
        except Exception as error:
            self.last_error = error
            logger.exception("RTC stopped unexpectedly")
            raise
        finally:
            self.connected = False
            self._changed.set()
            self._received.set()

    async def close(self): #reviewed
        #safety handler, cleaning stuff
        if self._close_task is None:
            self._closing = True
            self.connected = False
            self._changed.set()
            self._received.set()
            self._close_task = asyncio.create_task(self._close(asyncio.current_task()))
        await asyncio.shield(self._close_task)

    async def _close(self, caller=None): #reviewed
        # safety handler, avoids cancelling deadlock
        self._receiving = False
        if self._receive_task is not caller:
            await self.stop_receiving()
        await self._stop_direct()
        self.server_command = self.user_command = self._last_command = None
        if self._runner is not None: #run task does not involve close
            self._runner.cancel()
            await asyncio.gather(self._runner, return_exceptions=True)
