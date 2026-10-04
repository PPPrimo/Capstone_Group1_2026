from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from typing import TYPE_CHECKING

from aiortc import RTCPeerConnection, RTCDataChannel

if TYPE_CHECKING:
    from .robot_session import RobotSession

logger = logging.getLogger(__name__)


class ClientSession:
    def __init__(self, client_id: str, robot: RobotSession | None = None, on_disconnect: Callable[[ClientSession], Awaitable[None]] | None = None):
        self.client_id = client_id
        self.robot = robot
        self.pc = RTCPeerConnection()
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

                @channel.on("message")
                def on_message(message):
                    if not self._closing and self.robot is not None:
                        self.robot.send_control(message)

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

    def set_robot(self, robot: RobotSession):
        if self._closing or (self._video_track is not None and self.robot is not robot):
            raise RuntimeError("Close and recreate the client before changing its video source")
        self.robot = robot

    def attach_robot_video(self):
        if self._closing or self.robot is None:
            return False
        if self._video_track is not None:
            return self._video_track.readyState == "live"
        track = self.robot.get_video_track()
        if track is None:
            return False
        try:
            self.pc.addTrack(track)
        except Exception:
            track.stop()
            raise
        self._video_track = track
        return True

    def start_task(self, name: str, coroutine: Coroutine) -> asyncio.Task:
        existing = self.tasks.get(name)
        if self._closing or (existing is not None and not existing.done()):
            coroutine.close()
            raise RuntimeError(f"Cannot start client task {name!r}")
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
        if tasks:
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
