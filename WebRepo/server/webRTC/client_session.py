from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Coroutine
from typing import TYPE_CHECKING

from aiortc import RTCPeerConnection, RTCDataChannel
from aiortc.exceptions import InvalidStateError
from .ice_config import peer_configuration

if TYPE_CHECKING:
    from .robot_session import RobotSession

logger = logging.getLogger(__name__)


class ClientSession:
    def __init__(self, client_id: str, robot: RobotSession | None = None, on_disconnect: Callable[[ClientSession], Awaitable[None]] | None = None):
        self.client_id = client_id
        self.robot = robot
        self.pc = RTCPeerConnection(peer_configuration(f"client:{client_id}"))
        self.control_channel: RTCDataChannel | None = None
        self.connected = False
        self._closing = False
        self._close_task: asyncio.Task | None = None
        self._disconnect_task: asyncio.Task | None = None
        self._on_disconnect = on_disconnect
        self._video_track = None
        self.tasks: dict[str, asyncio.Task] = {}
        self._setup_handlers()

    def _setup_handlers(self):
        @self.pc.on("datachannel")
        def on_datachannel(channel):
            if channel.label == "teleop" and not self._closing:
                self.control_channel = channel

                channel.on("message", self._receive_control)

        @self.pc.on("connectionstatechange")
        async def on_connectionstatechange():
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

    async def _disconnect_after_delay(self):
        try:
            await asyncio.sleep(5)
            if self.pc.connectionState == "disconnected":
                self._start_close()
        finally:
            if self._disconnect_task is asyncio.current_task():
                self._disconnect_task = None

    def is_active(self):
        return (not self._closing and self.pc.connectionState == "connected"
                and self.control_channel is not None and self.control_channel.readyState == "open")

    def _receive_control(self, message):
        if not self.is_active():
            return
        try:
            control = json.loads(message)
            if not isinstance(control, dict) or control.get("master_mode") is not True:
                return
            command = json.dumps(control["command"], allow_nan=False)
        except (ValueError, KeyError, TypeError) as error:
            logger.warning("Invalid browser command: %s", error)
            return
        self.robot.send_control(command)

    def send_control(self, message):
        """Forward telemetry when the browser channel can accept it."""
        size = len(message.encode("utf-8")) if isinstance(message, str) else len(message)
        if not self.is_active() or self.control_channel.bufferedAmount + size > 65536:
            return False
        try:
            self.control_channel.send(message)
        except InvalidStateError:
            return False
        return True

    def attach_robot_video(self):
        if self._closing:
            return False
        track = self.robot.get_video_track()
        if track is None:
            return False
        self._video_track = track
        self.pc.addTrack(track)  # The negotiating caller closes this session on failure.
        return True

    def start_task(self, name: str, coroutine: Coroutine) -> asyncio.Task:
        task = asyncio.create_task(coroutine)
        self.tasks[name] = task
        task.add_done_callback(lambda done: self._task_done(name, done))
        return task

    def _task_done(self, name, task):
        if self.tasks.get(name) is task:
            self.tasks.pop(name, None)
        if not task.cancelled() and task.exception() is not None:
            logger.error("Client task %s failed", name, exc_info=task.exception())

    async def cancel_tasks(self) -> None:
        tasks = [task for task in self.tasks.values() if task is not asyncio.current_task() and not task.done()]
        self.tasks.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _start_close(self):
        if self._close_task is None:
            self._closing = True
            self.connected = False
            self._close_task = asyncio.create_task(self._close())
            self._close_task.add_done_callback(self._close_done)
        return self._close_task

    def _close_done(self, task):
        if not task.cancelled() and task.exception() is not None:
            logger.error("Client cleanup failed", exc_info=task.exception())

    async def close(self):
        task = self._start_close()
        if task is asyncio.current_task():
            return
        self.tasks = {name: other for name, other in self.tasks.items() if other is not asyncio.current_task()}
        await asyncio.shield(task)

    async def _close(self):
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
                if self._video_track is not None:
                    self._video_track.stop()
                    self._video_track = None
                try:
                    if self._on_disconnect is not None:
                        await self._on_disconnect(self)
                finally:
                    self.robot = None
