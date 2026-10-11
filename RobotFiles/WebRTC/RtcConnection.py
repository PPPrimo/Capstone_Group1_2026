import asyncio
import json
import inspect
import logging
import time
import uuid
from collections import deque
from urllib.parse import quote, urlsplit

import aiohttp
from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription

from .RtcVideo import SwitchableVideoTrack

logger = logging.getLogger(__name__)
ICE_SERVERS = [{"urls": "stun:stun.l.google.com:19302"}]
# Add TURN credentials here when required by your network.
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
        self.auxiliary_messages = {} #debug only
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
        self._commands = deque(maxlen=1) #recieved message from server
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
            except Exception as exc:
                logger.warning("Control send failed due to: %s", str(exc))
        return sent

    async def _next_control(self, timeout=None): #reviewed
        #chooses which command to follow (server overide or user)
        async with asyncio.timeout(timeout):
            while True:
                self._received.clear()
                if self._closing or (self._runner is not None and self._runner.done()):
                    raise self.last_error or RuntimeError("RTC is closed")
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
        self._commands.clear()
        self.server_command = self.user_command = self._last_command = None
        self._receiving = True
        self._receive_task = loop.create_task(self._dispatch_control(callback), name="robot-rtc-receiver")

    async def stop_receiving(self):
        #stops the recieving callback
        self._receiving = False
        self._commands.clear()
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
                except RuntimeError as exc:
                    logger.error("Closed or permanently failed connection. | %s", str(exc))
                    return
                if not self._receiving or self._closing:
                    return
                try:
                    result = callback(message)
                    if inspect.isawaitable(result):
                        await result
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception("Receive callback failed | %s", str(exc))
        finally:
            if self._receive_task is asyncio.current_task():
                self._receiving = False
                self._receive_task = None

    def _setup_handlers(self, pc):
        @pc.on("connectionstatechange")
        def on_state():
            if self.pc is pc and not self._closing:
                if pc.connectionState != "connected":
                    self.connected = False
                    self.server_command = self.user_command = self._last_command = None
                self._transport_changed.set()

        @pc.on("datachannel")
        def on_datachannel(channel):
            #avoids old reference, self cleaning in progress, or wrong channel
            if self.pc is not pc or self._closing or channel.label not in ("teleop", "auxiliary"):
                channel.close()
                return
            attribute = "control_channel" if channel.label == "teleop" else "auxiliary_channel"
            if getattr(self, attribute) is not None:
                channel.close()
                return
            setattr(self, attribute, channel)

            @channel.on("open")
            def on_open():
                self._transport_changed.set()

            @channel.on("close")
            def on_close():
                if self.pc is pc and not self._closing:
                    self.connected = False
                    self.server_command = self.user_command = self._last_command = None
                    self._transport_changed.set()

            @channel.on("message")
            def on_message(message):
                if self.pc is not pc or self._closing or pc.connectionState != "connected":
                    return
                if channel.label == "auxiliary":
                    self._receive_auxiliary(message)
                    return
                if not isinstance(message, (str, bytes)) or len(message) > MAX_COMMAND_BYTES:
                    return
                try:
                    envelope = json.loads(message)
                except (ValueError, TypeError) as exc:
                    logger.warning("mal-formated json | %s", str(exc))
                    return
                if not isinstance(envelope, dict) or envelope.get("identifier") != "CMD":
                    return
                source = envelope.get("source")
                command = envelope.get("command")
                if envelope.get("binary") is True:
                    import base64
                    try:
                        command = base64.b64decode(command, validate=True)
                    except (ValueError, TypeError) as exc:
                        logger.warning("command decode failed | %s", str(exc))
                        return
                if source == "server":
                    self._store_command(command, "server")
                elif source == "user" and self._direct is None:
                    self._store_command(command, "user")
            self._transport_changed.set()

    async def _negotiate(self, pc): #reviewed
        # Creating a bootstrap channel puts SCTP in the offer. The server creates teleop.
        pc.createDataChannel("_negotiation", ordered=False, maxRetransmits=0)
        async with asyncio.timeout(OFFER_TIMEOUT):
            await pc.setLocalDescription(await pc.createOffer())
        if pc.iceGatheringState != "complete":
            raise RuntimeError("ICE gathering did not complete")
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
                            raise RuntimeError("Signaling closed before the SDP answer")
                        answer = json.loads(message.data)
                        if not isinstance(answer, dict):
                            raise RuntimeError("Invalid signaling response")
                        if answer.get("type") == "error":
                            logger.warning("Server rejected RTC offer: %s", str(answer.get("error", "Unknown error")).replace(self.API_KEY, "[redacted]"))
                            raise RuntimeError("Server rejected RTC offer")
                        if answer.get("type") != "answer" or not isinstance(answer.get("sdp"), str):
                            raise RuntimeError("Invalid SDP answer")
                        if answer.get("request_id", request_id) != request_id:
                            raise RuntimeError("Answer request_id mismatch")
                        await pc.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type="answer"))
            # Signaling is intentionally finished here; the peer connection stays alive.
        except aiohttp.WSServerHandshakeError as exc:
            if exc.status in (401, 403):
                raise AuthenticationError("RTC authentication rejected; check API_KEY and server access") from None
            raise RuntimeError(f"Signaling handshake failed (HTTP {exc.status})") from None
        except aiohttp.ClientError:
            raise RuntimeError("Cannot reach RTC signaling server") from None

    async def _connection(self):
        #attempt 1 connection, check status upon statechange or 1 second, terminates after timeout
        pc = self.pc = RTCPeerConnection(RTCConfiguration(iceServers=[RTCIceServer(**item) for item in ICE_SERVERS]))
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
                    raise RuntimeError("RTC transport or control channel closed")
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
                        raise RuntimeError("RTC recovery timed out")
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
            self.auxiliary_messages.clear()
            self._finished_negotiations.clear()
            self.server_override = False
            self.server_command = self.user_command = self._last_command = None
            self.pc = None
            self.control_channel = None
            self.video_track = None
            self._commands.clear()
            track.stop()
            await pc.close()
            self._changed.set()

    def _store_command(self, message, source): #reviewed
        #checks msg before storing them
        if not isinstance(message, (str, bytes)):
            try:
                message = json.dumps(message, separators=(",", ":"), allow_nan=False)
            except (ValueError, TypeError) as exc:
                logger.warning("mal-formated json | %s", str(exc))
                return
        size = len(message.encode("utf-8")) if isinstance(message, str) else len(message)
        if size > MAX_COMMAND_BYTES:
            return
        value = (time.monotonic(), message)
        if source == "server":
            self.server_command = value
        elif source == "user":
            self.user_command = value
        else:
            return
        self._received.set()

    def _send_auxiliary(self, message): #reviewed
        channel = self.auxiliary_channel
        if self._closing or self.pc is None or self.pc.connectionState != "connected" or channel is None or channel.readyState != "open":
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
        except Exception as exc:
            logger.error("failed to send on aux channel | %s", str(exc))
            return False

    def _direct_message(self, state, identifier, **fields): #reviewed
        return {
            "identifier": identifier, 
            "negotiation_id": state["id"], 
            "session_id": state["session_id"], 
            **fields}

    def _receive_auxiliary(self, payload): #reviewed
        if not isinstance(payload, (str, bytes)) or len(payload) > 65536:
            return
        try:
            message = json.loads(payload)
            if not isinstance(message, dict):
                return
            identifier = message.get("ident")

            # This handler is only attached to the authenticated server connection.
            override = message.get("server_override")
            if type(override) is not bool:
                logger.warning("wrong server override format")
                return
            if override != self.server_override:
                self.server_override = override
                self.server_command = self.user_command = self._last_command = None
                self._received.set()
            #server override request
            if identifier == "SevOvrd":
                self.auxiliary_messages[identifier] = message
                return
            negotiation_id = message.get("ngoID")
            session_id = message.get("sessID")
            #checks format
            if not isinstance(negotiation_id, str) or not 1 <= len(negotiation_id) <= 128 or not all(c.isalnum() or c in "_-" for c in negotiation_id):
                logger.warning("malformated negotiation id")
                return
            if not isinstance(session_id, str) or not 1 <= len(session_id) <= 128:
                logger.warning("incorrect session id")
                return
            key = (session_id, negotiation_id)
            state = self._direct
            if key in self._finished_negotiations: 
                #the offer send after robot complets nego but before server recieved the confirmation,should be ignore it
                return
            if identifier == "R_Off": #request to start the offer relay procedure
                if state is not None:
                    if (state["session_id"], state["id"]) != key:
                        error = {"id": negotiation_id, "session_id": session_id}
                        self._send_auxiliary(self._direct_message(error, "R_Err", error="busy"))
                    elif not state.get("established"): #informs server of recieved offer and is under processing
                        self._send_auxiliary(self._direct_message(state, "R_Rdy", ready=True))
                        if state.get("answer") is not None:
                            self._send_auxiliary(self._direct_message(state, "R_Ans", answer=state["answer"]))
                    return
                if not self.connected:
                    error = {"id": negotiation_id, "session_id": session_id}
                    self._send_auxiliary(self._direct_message(error, "R_E", error="robot_not_ready"))
                    return
                offer = message.get("offer")
                if not isinstance(offer, dict) or offer.get("type") != "offer" or not isinstance(offer.get("sdp"), str) or len(offer["sdp"].encode("utf-8")) > 48000:
                    return
                state = {"id": negotiation_id, "session_id": session_id, "offer": offer, "changed": asyncio.Event(), "answer_ack": False, "status_ack": None, "cancelled": False, "established": False, "pc": None, "channel": None, "track": None}
                self._direct = state
                self.user_command = None
                self.auxiliary_messages[identifier] = message
                self._send_auxiliary(self._direct_message(state, "R_Rdy", ready=True))
                self._direct_task = asyncio.create_task(self._run_direct(state), name=f"direct:{negotiation_id}") #process the offer
                return
            if state is None or (state["session_id"], state["id"]) != key:
                return
            if identifier == "R_Ack":
                state["answer_ack"] = True
            elif identifier == "R_Cnf" and message.get("ClntCtrl") in ("T", "F"):
                state["status_ack"] = message["client_ctrl"]
            elif identifier == "R_Cnl":
                state["cancelled"] = True
            if not state["established"]:
                self.auxiliary_messages[identifier] = message
            state["changed"].set()
        except (ValueError, TypeError, KeyError) as exc:
            logger.error("auxiliar channel error | %s", exc)
            return

    def _setup_direct_handlers(self, state): #reviewed
        pc = state["pc"]

        @pc.on("connectionstatechange")
        def on_state(): #new
            if pc.connectionState != "connected":
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
                if self._direct is state and state["established"] and pc.connectionState == "connected" and not state["cancelled"]:
                    self._store_command(message, "user") #direct channel recieves command from user only
            state["changed"].set()

    async def _run_direct(self, state): #reviewed
        pc = state["pc"] = RTCPeerConnection(RTCConfiguration(iceServers=[RTCIceServer(**item) for item in ICE_SERVERS]))
        self._setup_direct_handlers(state)
        negotiating = None
        try:
            negotiating = asyncio.create_task(self._answer_direct(state))
            async with asyncio.timeout(OFFER_TIMEOUT):
                while not negotiating.done(): #repeat informs server R_Rdy untill the negotiation is done
                    state["changed"].clear()
                    if state["cancelled"] or not self.connected:
                        raise RuntimeError("Direct negotiation cancelled")
                    self._send_auxiliary(self._direct_message(state, "R_Rdy", ready=True))
                    await asyncio.wait({negotiating}, timeout=0.5)
                state["answer"] = await negotiating
            deadline = time.monotonic() + CONNECT_TIMEOUT
            disconnected_at = None
            while True:
                state["changed"].clear()
                if state["cancelled"] or not self.connected or self._direct is not state:
                    break
                channel = state["channel"]
                if pc.connectionState in ("failed", "closed") or (channel is not None and channel.readyState == "closed"):
                    break
                ready = pc.connectionState == "connected" and channel is not None and channel.readyState == "open" and state["video_negotiated"]
                if ready:
                    disconnected_at = None
                    if not state["established"]:
                        state["established"] = True
                        self.auxiliary_messages = {k: v for k, v in self.auxiliary_messages.items() if v.get("negotiation_id") != state["id"]}
                    if state["status_ack"] != "T": #server has not confirmed full connection
                        self._send_auxiliary(self._direct_message(state, "R_Sts", client_ctrl="T"))
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
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._send_auxiliary(self._direct_message(state, "R_Err", error=str(exc) or type(exc).__name__))
        finally:
            if negotiating is not None:
                if not negotiating.done():
                    negotiating.cancel()
                await asyncio.gather(negotiating, return_exceptions=True)
            await pc.close()
            if state["track"] is not None:
                state["track"].stop()
            self.user_command = None
            # Announce release until acknowledged; reliable SCTP also preserves ordering.
            if not self._closing and self.connected and not state["cancelled"]:
                for _ in range(6):
                    if state["status_ack"] == "F":
                        break
                    state["changed"].clear()
                    self._send_auxiliary(self._direct_message(state, "R_Sts", client_ctrl="F"))
                    try:
                        await asyncio.wait_for(state["changed"].wait(), 0.5)
                    except TimeoutError:
                        pass
            self._finished_negotiations[(state["session_id"], state["id"])] = time.monotonic()
            while len(self._finished_negotiations) > 256:
                self._finished_negotiations.pop(next(iter(self._finished_negotiations)))
            self.auxiliary_messages = {k: v for k, v in self.auxiliary_messages.items() if v.get("negotiation_id") != state["id"]}
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
                except asyncio.CancelledError:
                    raise
                except AuthenticationError as exc:
                    self.last_error = exc
                    logger.error("%s", exc)
                    return
                except Exception as exc:
                    self.last_error = RuntimeError(f"RTC attempt failed ({type(exc).__name__})")
                    logger.warning("%s; reconnecting", self.last_error)
                if time.monotonic() - started > 60:
                    delay = 0.5
                if not self._closing:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 4)
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
        self._commands.clear()