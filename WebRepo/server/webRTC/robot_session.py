from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Coroutine

from aiortc import RTCPeerConnection, RTCDataChannel
from aiortc.contrib.media import MediaRelay
from aiortc.exceptions import InvalidStateError
from .ice_config import peer_configuration

logger = logging.getLogger(__name__)


class RobotSession:
    def __init__(self, 
        robot_id: str,
        robot_role: str,
        relay: MediaRelay, 
        on_disconnect: Callable[[RobotSession], Awaitable[None]], 
        on_video_lost=None,
        on_control=None,
        on_status=None,
        ):

        self.robot_id = robot_id
        self.robot_role = robot_role
        self.logger_start = f"id: {robot_id} | role: {robot_role} - "
        self.pc = RTCPeerConnection(peer_configuration(f"robot:{robot_id}"))
        self.control_channel: RTCDataChannel | None = None
        self.auxiliary_channel: RTCDataChannel | None = None
        self.direct_pending = None
        self.direct_requests = {}
        self.client_ctrl = "F"
        self.server_override = False
        self.video_track = None
        self.relay = relay
        self.on_disconnect = on_disconnect
        self.on_video_lost = on_video_lost
        self.on_control = on_control
        self.on_status = on_status
        self.status = None
        self.last_control_message: str | bytes | None = None
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
            if state == "connected":
                if self._disconnect_task is not None:
                    self._disconnect_task.cancel()
                    self._disconnect_task = None
                await self.set_status("connected")
            elif state == "disconnected":
                if self._disconnect_task is None:
                    self._disconnect_task = asyncio.create_task(self._disconnect_after_delay())
                await self.set_status("lost")
            elif state in ("failed", "closed"):
                self._start_close()
                if state == "failed":
                    await self.set_status("lost")

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

    async def set_status(self, status):
        if self.status != status:
            self.status = status
            if self.on_status is not None:
                await self.on_status(self, status)

#helpers
    def is_active(self, channel="control"):
        channels = {"control": (self.control_channel,), "auxiliary": (self.auxiliary_channel,),
                    "both": (self.control_channel, self.auxiliary_channel)}[channel]
        return not self._closing and self.pc.connectionState == "connected" and all(
            item is not None and item.readyState == "open" for item in channels)

    def _negotiation(self, message):
        if message["sessID"] != self.session_id:
            raise ValueError("Message belongs to another robot session")
        state = self.direct_requests.get(message["ngoID"])
        if state is None or state.get("ended"):
            return None  # Late acknowledgement for an expired/replaced negotiation.
        if state["connected"].is_set() and message["ident"] != "R_Sts":
            return None
        return state

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
        if source not in ("user", "server"):
            raise ValueError("Unknown command source")
        binary = isinstance(message, bytes)
        if binary:
            message = base64.b64encode(message).decode("ascii")
        payload = json.dumps({
            "identifier": identifier, 
            "source": source, 
            "binary": binary, 
            "command": message}, 
            allow_nan=False)
        if len(payload.encode("utf-8")) > 16384:
            raise ValueError("Control message exceeds 16 KiB")
        return payload
    
    def _receive_auxiliary(self, payload):
        if self._closing:
            return
        message = json.loads(payload)
        state = self._negotiation(message)
        if state is None:
            return
        identifier = message["ident"]
        if identifier in ("R_Rdy", "R_Ans") and self.direct_pending is not state:
            previous = self.direct_pending
            if previous is not None:
                previous.update(error="Connection replaced by another user", ended=True)
                previous["changed"].set()
                self.direct_requests.pop(previous["id"], None)
            self.direct_pending = state
            self.client_ctrl = "F"
        if identifier == "R_Sts":
            status = message["clntCtrl"]
            if status == "T" and not state["answer"].done():
                raise ValueError("Robot reported connected before answering")
            self.client_ctrl = status
            self.send_auxiliary({"ident": "R_Cnf", "ngoID": state["id"], "sessID": self.session_id, "clntCtrl": status})
            if status == "T":
                state["connected"].set()
            else:
                state.update(error="Direct client disconnected", ended=True)
                self.direct_requests.pop(state["id"], None)
        elif identifier == "R_Rdy":
            state["received"].set()
        elif identifier == "R_Err":
            state["error"] = message["error"]
        elif identifier == "R_Ans":
            state["received"].set()
            if not state["answer"].done():
                state["answer"].set_result(message["answer"])
        else:
            raise ValueError(f"Unknown robot message: {identifier}")
        state["changed"].set()

# control functions
    def _receive_control(self, message):
        if self._closing:
            return
        self.last_control_message = message
        if self.on_control is not None:
            self.on_control(self, message)

    def create_control_channel(self): #reviewed
        if self._closing:
            raise RuntimeError("Robot session is closing")
        if self.control_channel is None:
            self.control_channel = self.pc.createDataChannel("teleop", ordered=False, maxRetransmits=0)
            self.control_channel.on("message", self._receive_control)
        if self.auxiliary_channel is None:
            self.auxiliary_channel = self.pc.createDataChannel("auxiliary", ordered=True)
            self.auxiliary_channel.on("message", self._receive_auxiliary)
            self.auxiliary_channel.on("open", lambda: self.set_server_override(self.server_override))
        return self.control_channel

    def send_control(self, message, source="user"): #reviewed
        payload = self._prepare_RTC_message("CMD", source, message)
        if self.is_active():
            self.control_channel.send(payload)
            return True
        return False

    def send_auxiliary(self, message):
        if not self.is_active("auxiliary"):
            return False
        payload = json.dumps({**message, "server_override": self.server_override}, separators=(",", ":"), allow_nan=False)
        size = len(payload.encode("utf-8"))
        if size > 65536:
            raise ValueError("Auxiliary message exceeds 64 KiB")
        if self.auxiliary_channel.bufferedAmount + size > 262144:
            return False
        try:
            self.auxiliary_channel.send(payload)
        except InvalidStateError:
            return False  # Transport closed between checking and sending.
        return True

    def set_server_override(self, enabled): #reviewed
        if type(enabled) is not bool:
            raise ValueError("server_override must be a boolean")
        self.server_override = enabled
        return self.send_auxiliary({"ident": "S_ovr"})

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
        for state in self.direct_requests.values():
            state["error"] = "Robot disconnected"
            state["changed"].set()
        self.client_ctrl = "F"
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
                self.last_control_message = None
                self.auxiliary_channel = None
                self.direct_pending = None
                self.direct_requests.clear()
                track = self.video_track
                self.video_track = None
                if track is not None:
                    track.stop()
                try:
                    await self.set_status("confirmed_lost")
                finally:
                    await self._notify_disconnect()

    def _close_done(self, task): #reviewed
        if not task.cancelled() and task.exception() is not None:
            logger.error("Robot cleanup failed", exc_info=task.exception())
