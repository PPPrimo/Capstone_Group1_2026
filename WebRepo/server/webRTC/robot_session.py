from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable, Coroutine

from aiortc import RTCPeerConnection, RTCDataChannel
from aiortc.contrib.media import MediaRelay

logger = logging.getLogger(__name__)


class RobotSession:
    def __init__(self, 
        robot_id: str,
        robot_role: str,
        relay: MediaRelay, 
        on_disconnect: Callable[[RobotSession], Awaitable[None]], 
        on_video_lost=None
        ):

        self.robot_id = robot_id
        self.robot_role = robot_role
        self.logger_start = f"id: {robot_id} | role: {robot_role} - "
        self.pc = RTCPeerConnection()
        self.control_channel: RTCDataChannel | None = None
        self.auxiliary_channel: RTCDataChannel | None = None
        self.auxiliary_messages = {}
        self.direct_pending = None
        self.owns_robot: Callable[[], bool] = lambda: False
        self.client_ctrl = "F"
        self.server_override = False
        self.video_track = None
        self.relay = relay
        self.on_disconnect = on_disconnect
        self.on_video_lost = on_video_lost
        self.session_id = uuid.uuid4().hex
        self.video_ready = asyncio.Event()
        self._video_changed = asyncio.Event()
        self._disconnect_task: asyncio.Task | None = None
        self._close_task: asyncio.Task | None = None
        self.tasks: dict[str, asyncio.Task] = {}
        self.connected = False
        self._closing = False
        self._disconnect_notified = False
        self.ready_key_value: str | None = None
        self.ready_key: str | None = None
        self.offer_channel: str | None = None
        self.answer_channel: str | None = None
        self._setup_handlers()

    def _setup_handlers(self):
        @self.pc.on("connectionstatechange")
        async def on_connectionstatechange(): #reviewed
            state = self.pc.connectionState
            self.connected = state == "connected" and not self._closing
            if self._closing:
                return
            if state == "connected" and self._disconnect_task is not None:
                self._disconnect_task.cancel()
            elif state == "disconnected" and self._disconnect_task is None:
                self._disconnect_task = asyncio.create_task(self._disconnect_after_delay())
            elif state in ("failed", "closed"):
                self._start_close()

        @self.pc.on("track")
        def on_track(track): #reviewed
            if track.kind != "video" or self._closing:
                return
            old_track = self.video_track
            self.video_track = track
            self.video_ready.set()
            self._video_changed.set()
            if old_track is not None and old_track is not track:
                old_track.stop()
                self._notify_video_lost()

            @track.on("ended")
            def on_ended(): #reviewed
                if self.video_track is track:
                    self.video_track = None
                    self.video_ready.clear()
                    self._video_changed.set()
                    self._notify_video_lost()

#helpers
    def direct_ready(self): #reviewed
        return self._can_control("both")
    
    def _can_control(self, channel="control") -> bool: #reviewed
        if channel not in ("control", "auxiliary", "both"):
            raise ValueError("Unknown channel selection")

        is_active = not self._closing
        is_connected = self.pc.connectionState == "connected"
        control_ready = self.control_channel is not None and self.control_channel.readyState == "open"
        auxiliary_ready = self.auxiliary_channel is not None and self.auxiliary_channel.readyState == "open"
        owns_robot = self.owns_robot()
        if channel == "control":
            channel_ready = control_ready
        elif channel == "auxiliary":
            channel_ready = auxiliary_ready
        else:
            channel_ready = control_ready and auxiliary_ready

        result = is_active and is_connected and channel_ready
        return result

    def _notify_video_lost(self): #reviewed
        if not self._closing and self.on_video_lost is not None:
            self.start_task(f"video_lost:{uuid.uuid4().hex}", self.on_video_lost(self))

    async def _notify_disconnect(self): #reviewed
        if not self._disconnect_notified:
            await self.on_disconnect(self)
            self._disconnect_notified = True

    async def _disconnect_after_delay(self): #reviewed
        try:
            await asyncio.sleep(5)
            if self.pc.connectionState == "disconnected":
                self._start_close()
        finally:
            if self._disconnect_task is asyncio.current_task():
                self._disconnect_task = None

    def _prepare_RTC_message(self, identifier, source, message):
        import json
        if source not in ("user", "server"):
            raise ValueError("Unknown command source")
        binary = isinstance(message, bytes)
        if binary:
            import base64
            message = base64.b64encode(message).decode("ascii")
        payload = json.dumps({
            "identifier": identifier, 
            "source": source, 
            "binary": binary, 
            "command": message}, 
            allow_nan=False)
        if len(payload.encode("utf-8")) > 16384:
            logger.warning(self.logger_start+"oversized payload")
            return False
        return payload
    
    def _receive_auxiliary(self, payload): #reviewed, need to repurpose to allow multi identifier
        "R_Off: Relay_Offer, R_Ans: Relay_Answer, R_Sts: Relay_Status, R_Err: Relay_Error, R_Cnf: Relay_Confirmation"
        import json
        if not isinstance(payload, (str, bytes)) or len(payload) > 65536:
            return
        try:
            message = json.loads(payload)
            if not isinstance(message, dict):
                return
            state = self.direct_pending
            identifier = message.get("ident")
            #duplication check
            if state is None or message.get("ngoID") != state["id"] or message.get("sessID") != self.session_id:
                logger.warning(self.logger_start+"negotiation or session id missmatch")
                return
            #processing
            if identifier == "R_Sts":
                status = message.get("clntCtrl")
                if status not in ("T", "F"):
                    logger.warning(self.logger_start+"invalid connection status response")
                    return
                if status == "T" and not state["answer"].done():
                    logger.info(self.logger_start+"ignored message, a negotiation is active")
                    return
                #confirmed direct control answer recieved
                self.client_ctrl = status
                self.send_auxiliary({"ident": "R_Cnf", "ngoID": state["id"], "sessID": self.session_id, "clntCtrl": status})
                if status == "T":
                    state["connected"].set()
                    self.auxiliary_messages.clear()
                else:
                    state["error"] = "Direct client disconnected"
                    state["ended"] = True
                state["changed"].set()
                return
            if state["connected"].is_set() or state.get("ended"):
                logger.info(self.logger_start+"ignored message, negotiation ended or completed")
                return
            if identifier == "R_Off" and message.get("ready") is True:
                state["received"].set()
            elif identifier == "R_Err":
                state["error"] = str(message.get("error", "Robot rejected negotiation"))
                logger.warning(self.logger_start+"Robot rejected negotiation")
            elif identifier == "R_Ans":
                answer = message.get("answer")
                if not isinstance(answer, dict) or answer.get("type") != "answer" or not isinstance(answer.get("sdp"), str) or len(answer["sdp"].encode("utf-8")) > 48000 or not answer["sdp"].startswith("v=0"):
                    return
                # A valid answer is also an implicit receipt/readiness confirmation.
                state["received"].set()
                if not state["answer"].done():
                    state["answer"].set_result(answer)
            state["changed"].set()
            self.auxiliary_messages[identifier] = message #redundent

        except (ValueError, TypeError, KeyError):
            return

# control functions
    def create_control_channel(self): #reviewed
        if self._closing:
            raise RuntimeError("Robot session is closing")
        if self.control_channel is None:
            self.control_channel = self.pc.createDataChannel("teleop", ordered=False, maxRetransmits=0)
        if self.auxiliary_channel is None:
            self.auxiliary_channel = self.pc.createDataChannel("auxiliary", ordered=True)
            self.auxiliary_channel.on("message", self._receive_auxiliary)
            self.auxiliary_channel.on("open", lambda: self.set_server_override(self.server_override))
        return self.control_channel

    def send_control(self, message, source="user"): #reviewed
        payload = self._prepare_RTC_message("CMD", source, message)
        if self._can_control() and payload is not False:
            self.control_channel.send(payload)
            return True
        return False

    def send_auxiliary(self, message): #To Be Redesigned
        import json
        channel = self.auxiliary_channel
        if not self._can_control("auxiliary"):
            logger.info(self.logger_start+"can't control")        
            return False
        payload = json.dumps({**message, "server_override": self.server_override}, separators=(",", ":"), allow_nan=False)
        size = len(payload.encode("utf-8"))
        if size > 65536:
            raise ValueError("Auxiliary message exceeds 64 KiB")
        if channel.bufferedAmount + size > 262144:
            logger.warning(self.logger_start+"oversized aux message")
            return False
        try:
            channel.send(payload)
            return True
        except Exception as error:
            logger.critical(self.logger_start+"send_auxiliary() - "+ str(error))
            return False
        
    def set_server_override(self, enabled): #reviewed
        if type(enabled) is not bool:
            logger.error(self.logger_start+"invalid server override format")
            raise ValueError("server_override must be a boolean")
        self.server_override = enabled
        return self.send_auxiliary({"ident": "SO"})

# video functions
    async def wait_for_video(self, timeout: float = 10): #reviewed
        async with asyncio.timeout(timeout):
            while True:
                if self._closing:
                    raise RuntimeError("Robot closed before video became available")
                if self.video_track is not None and self.video_track.readyState == "live":
                    return self.video_track
                self._video_changed.clear()
                await self._video_changed.wait()

    def get_video_track(self): #unused - client connection case does not exist anymore
        if self._closing or self.video_track is None or self.video_track.readyState != "live":
            return None
        return self.relay.subscribe(self.video_track)

# task helpers
    def start_task(self, name: str, coroutine: Coroutine) -> asyncio.Task: #reviewed
        existing = self.tasks.get(name)
        if self._closing or (existing is not None and not existing.done()):
            coroutine.close()
            raise RuntimeError(f"Task already existed or robot is closing {name!r}")
        task = asyncio.create_task(coroutine)
        self.tasks[name] = task
        task.add_done_callback(lambda done: self._task_done(name, done))
        return task

    def _task_done(self, name, task): #reviewed
        if self.tasks.get(name) is task:
            self.tasks.pop(name, None)
        if not task.cancelled() and task.exception() is not None:
            logger.error("Robot task %s failed", name, exc_info=task.exception())

    async def cancel_tasks(self) -> None: #reviewed
        tasks = [task for task in self.tasks.values() if task is not asyncio.current_task() and not task.done()]
        self.tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

# termination
    async def close(self): #reviewed
        task = self._start_close()
        if task is asyncio.current_task():
            return
        self.tasks = {
            name: other 
            for name, other in self.tasks.items() 
            if other is not asyncio.current_task()}
        await asyncio.shield(task)

    def _start_close(self): #reviewed
        if self._close_task is None:
            self._closing = True
            self.connected = False
            self.video_ready.clear()
            self._video_changed.set()
            self._close_task = asyncio.create_task(self._close())
            self._close_task.add_done_callback(self._close_done)
        return self._close_task

    async def _close(self): #reviewed
        state = self.direct_pending
        if state is not None:
            state["error"] = "Robot disconnected"
            state["changed"].set()
        self.client_ctrl = "F"
        self.auxiliary_messages.clear()
        try:
            timer = self._disconnect_task
            self._disconnect_task = None
            if timer is not None and timer is not asyncio.current_task():
                timer.cancel()
                await asyncio.gather(timer, return_exceptions=True)
            await self.cancel_tasks()
        finally:
            try:
                await self.pc.close()
            finally:
                self.control_channel = None
                self.auxiliary_channel = None
                self.direct_pending = None
                track = self.video_track
                self.video_track = None
                if track is not None:
                    track.stop()
                await self._notify_disconnect()

    def _close_done(self, task): #reviewed
        if not task.cancelled() and task.exception() is not None:
            logger.error("Robot cleanup failed", exc_info=task.exception())
