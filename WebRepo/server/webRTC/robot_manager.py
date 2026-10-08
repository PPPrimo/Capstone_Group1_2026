from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Coroutine
from urllib.parse import quote

from aiortc.contrib.media import MediaRelay

from .client_session import ClientSession
from .robot_session import RobotSession

logger = logging.getLogger(__name__)
DELETE_IF_OWNER = "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) else return 0 end"


@dataclass
class RobotManager:
    robot_id: str
    relay: MediaRelay
    robot_devs: dict[str, RobotSession] = field(default_factory=dict)
    client_devs: dict[str, ClientSession] = field(default_factory=dict)
    redis: Any | None = None
    owner_key: str = ""
    owner_key_value: str = ""
    announce_key: str = ""
    offer_channel: str = ""
    active_connections: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    prepare_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    offer_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    requests: dict[str, tuple] = field(default_factory=dict)
    sub_ready: asyncio.Event = field(default_factory=asyncio.Event)
    last_activity: float = field(default_factory=time.monotonic)
    lease_deadline: float = 0
    pending_offers: int = 0
    _closing: bool = False
    logger_start = str

# configurations
    def configure_redis(self, redis, robot_id, robot_role=None): #reviewed
        self.logger_start = f"id: {robot_id} | "
        self.redis = redis
        key_id = quote(robot_id, safe="")
        self.owner_key = f"rtc:user:owner:{key_id}"
        self.announce_key = f"rtc:user:announce:{key_id}"
        self.offer_channel = f"rtc:user:offer:{key_id}"
        if robot_role is None:
            return
        robot_session = self.robot_devs.get(robot_role)
        if robot_session is None:
            raise RuntimeError("Create the robot session before configuring its role keys")
        robot_session.ready_key = f"rtc:user:ready:{key_id}:{quote(robot_role, safe='')}"
        robot_session.ready_key_value = f"{self.owner_key_value}:{robot_session.session_id}"
        robot_session.owns_robot = self.owns_lease

    def owns_lease(self): #reviewed
        return not self._closing and bool(self.owner_key_value) and time.monotonic() < self.lease_deadline

    def _update_connections(self): #reviewed
        self.active_connections = len(self.robot_devs) + len(self.client_devs)
        self.last_activity = time.monotonic()

    async def create_robot_session(self, robot_role: str) -> RobotSession: #reviewed
        async with self.lock:
            if self._closing:
                raise RuntimeError("Robot manager is closing")
            existing_robot = self.get_robot_session(robot_role)
            existing_client = self.client_devs.pop(robot_role, None)
            disconnect_callback = partial(self.remove_robot_session, robot_role)
            video_callback = partial(self.handle_video_lost, robot_role)
            robot_session = RobotSession(
                robot_id= self.robot_id,
                robot_role= robot_role,
                relay=self.relay, 
                on_disconnect=disconnect_callback, 
                on_video_lost=video_callback)
            robot_session.create_control_channel()
            self.robot_devs[robot_role] = robot_session
            self.configure_redis(self.redis, self.robot_id, robot_role)
            self._update_connections()
        try:
            await self._close_sessions(existing_client, existing_robot)
            return robot_session
        except BaseException:
            await robot_session.close()
            raise

    async def create_client_session(self, robot_role: str) -> ClientSession:
        async with self.lock:
            robot_session = self.get_robot_session(robot_role)
            if self._closing or robot_session is None or robot_session._closing:
                raise RuntimeError(f"No live robot session exists for role {robot_role!r}")
            old_client = self.get_client_session(robot_role)
            disconnect_callback = partial(self.remove_client_session, robot_role)
            client = ClientSession(client_id=uuid.uuid4().hex, robot=robot_session, on_disconnect=disconnect_callback)
            self.client_devs[robot_role] = client
            self._update_connections()
        try:
            if old_client is not None:
                await old_client.close()
            return client
        except BaseException:
            await client.close()
            raise

# functional helpers
    def get_robot_session(self, robot_role: str) -> RobotSession | None: #reviewed
        return self.robot_devs.get(robot_role)

    def get_client_session(self, robot_role: str) -> ClientSession | None:
        return self.client_devs.get(robot_role)

    async def remove_robot_session(self, robot_role: str, robot: RobotSession): #reviewed
        async with self.lock:
            current = self.get_robot_session(robot_role)
            client = None
            if current is robot:
                self.robot_devs.pop(robot_role, None)
                client = self.client_devs.pop(robot_role, None)
                self._update_connections()
        # Always clean this robot's resources, even if a replacement is registered.
        try:
            await self._close_sessions(client, robot)
        finally:
            await self.clear_ready(robot)

    async def remove_client_session(self, robot_role: str, client: ClientSession):
        async with self.lock:
            if self.get_client_session(robot_role) is client:
                self.client_devs.pop(robot_role, None)
                self._update_connections()
        await client.close()

    async def handle_video_lost(self, robot_role: str, robot: RobotSession): #reviewed
        async with self.lock:
            client = self.client_devs.pop(robot_role, None) if self.get_robot_session(robot_role) is robot else None
            self._update_connections()
        try:
            if client is not None:
                await client.close()
        finally:
            await self.clear_ready(robot)

    async def clear_ready(self, robot: RobotSession): #reviewed
        if self.redis is not None and robot.ready_key and robot.ready_key_value:
            try:
                async with asyncio.timeout(3):
                    await self.redis.eval(DELETE_IF_OWNER, 1, robot.ready_key, robot.ready_key_value)
            except Exception:
                logger.exception("Could not clear robot readiness; its TTL will expire")

    async def _close_sessions(self, *sessions): #reviewed
        # Sequential calls preserve close-task identity when a callback re-enters the manager.
        error = None
        for session in sessions:
            if session is not None:
                try:
                    await session.close()
                except Exception as exc:
                    error = exc
        if error is not None:
            raise error

    async def close_sessions(self): #reviewed
        async with self.lock:
            sessions = [*self.client_devs.values(), *self.robot_devs.values()]
            self.client_devs.clear()
            self.robot_devs.clear()
            self._update_connections()
        await self._close_sessions(*sessions)

# task helpers start
    def start_task(self, name: str, coroutine: Coroutine) -> asyncio.Task: #reviewed
        existing = self.tasks.get(name)
        if self._closing or (existing is not None and not existing.done()):
            coroutine.close()
            raise RuntimeError(f"Cannot start manager task {name!r}")
        task = asyncio.create_task(coroutine)
        self.tasks[name] = task
        task.add_done_callback(lambda done: self._task_done(name, done))
        return task

    def _task_done(self, name, task): #reviewed
        if self.tasks.get(name) is task:
            self.tasks.pop(name, None)
        if not task.cancelled() and task.exception() is not None:
            logger.error("Manager task %s failed", name, exc_info=task.exception())

    async def cancel_tasks(self): #reviewed
        tasks = [task for task in self.tasks.values() if task is not asyncio.current_task() and not task.done()]
        self.tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
# task helpers start

#direct connection feature start
    async def _confirm_direct(self, robot, state): #reviewed
        try:
            async with asyncio.timeout(35):
                while not state["connected"].is_set():
                    state["changed"].clear()
                    if robot.direct_pending is not state or state["error"] or not robot.direct_ready():
                        raise RuntimeError("Direct negotiation ended")
                    robot.send_auxiliary({"ident": "R_Ack", "ngoID": state["id"], "sessID": robot.session_id})
                    try:
                        await asyncio.wait_for(state["changed"].wait(), 0.5)
                    except TimeoutError:
                        pass
        except (TimeoutError, RuntimeError) as error:
            logger.error(self.logger_start + "cancel direct negotiation due to: " + str(error))
            self._cancel_direct(robot, state)
        except asyncio.CancelledError:
            logger.critical(self.logger_start + "cancel direct negotiation, a asyncio cancelled error happened")
            self._cancel_direct(robot, state)
            raise

    def _cancel_direct(self, robot, state): #reviewed
        robot.send_auxiliary({"ident": "R_Cnl", "ngoID": state["id"], "sessID": robot.session_id})
        if robot.direct_pending is state:
            state["ended"] = True
            robot.direct_pending = None
            robot.client_ctrl = "F"
            robot.auxiliary_messages.clear()
        if not state["answer"].done():
            state["answer"].cancel()

    async def relay_user_offer(self, robot_role, offer, request_id, timeout=20): #reviewed
        robot = self.get_robot_session(robot_role)
        if robot is None or not self.owns_lease() or not robot.direct_ready():
            raise RuntimeError("robot_not_ready")
        if robot.direct_pending is not None and not robot.direct_pending.get("ended"):
            raise RuntimeError("busy")
        state = {
            "id": request_id, 
            "answer": asyncio.get_running_loop().create_future(), 
            "received": asyncio.Event(), 
            "connected": asyncio.Event(), 
            "changed": asyncio.Event(), 
            "error": None
            }
        robot.direct_pending = state
        robot.auxiliary_messages.clear()
        message = {
            "ident": "R_Off", 
            "ngoID": request_id, 
            "sessID": robot.session_id, 
            "offer": {"type": "offer", "sdp": offer["sdp"]}
            }
        try:
            async with asyncio.timeout(timeout):
                while True:
                    #session checks
                    state["changed"].clear()
                    if self.get_robot_session(robot_role) is not robot or not self.owns_lease() or not robot.direct_ready():
                        raise RuntimeError("robot_not_ready")
                    if state["error"]:
                        raise RuntimeError(state["error"])
                    #process
                    if state["answer"].done():
                        answer = state["answer"].result()
                        robot.start_task(f"direct:{request_id}", self._confirm_direct(robot, state))
                        return answer
                    if not state["received"].is_set():
                        robot.send_auxiliary(message)
                    
                    try:
                        await asyncio.wait_for(state["changed"].wait(), 0.5)
                    except TimeoutError:
                        pass
        except BaseException:
            self._cancel_direct(robot, state)
            raise
#direct connection feature end