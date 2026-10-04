from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from aiortc.contrib.media import MediaRelay

from .robot_manager import RobotManager, DELETE_IF_OWNER

logger = logging.getLogger(__name__)
REFRESH_IF_OWNER = "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('EXPIRE', KEYS[1], ARGV[2]) else return 0 end"
SET_IF_OWNER = "if redis.call('GET', KEYS[1]) == ARGV[1] then redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[3]); return 1 else return 0 end"
PUBLISH_IF_OWNER = "if redis.call('GET', KEYS[1]) == ARGV[1] then redis.call('PUBLISH', KEYS[2], ARGV[2]); return 1 else return 0 end"


@dataclass
class WebRTCManager:
    worker_id: str
    robot_lists: dict[str, RobotManager] = field(default_factory=dict)
    relay: MediaRelay = field(default_factory=MediaRelay)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    redis: Any | None = None
    lease_ttl: int = 30
    offer_timeout: float = 20
    request_timeout: float = 30
    connect_timeout: float = 30
    idle_timeout: float = 30
    request_cache_ttl: float = 120
    _retire_tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    _closing: bool = False

    def create_robot_manager(self, robot_id: str) -> RobotManager: #reviewed
        # Keep the original spelling for existing callers.
        manager = self.robot_lists.get(robot_id)
        if manager is None:
            manager = RobotManager(robot_id=robot_id, relay=self.relay)
            manager.configure_redis(self.redis, robot_id)
            self.robot_lists[robot_id] = manager
        return manager


    def get_robot_manager(self, robot_id: str) -> RobotManager | None: #reviewed
        return self.robot_lists.get(robot_id)

    async def is_owner(self, manager): #reviewed
        if not manager.owns_lease():
            return False
        async with asyncio.timeout(3):
            return await self.redis.get(manager.owner_key) == manager.owner_key_value

    async def acquire_worker(self, robot_id: str) -> bool: #reviewed
        if self.redis is None or self._closing:
            raise RuntimeError("Redis is unavailable or this worker is closing")
        async with self.lock:
            manager = self.create_robot_manager(robot_id)
            if manager._closing:
                return False
            if manager.owner_key_value:
                if await self.is_owner(manager):
                    return True
                self._start_retire(manager)
                return False
            token = f"{self.worker_id}:{uuid.uuid4().hex}"
            started = time.monotonic()
            async with asyncio.timeout(3):
                claimed = await self.redis.set(manager.owner_key, token, nx=True, ex=self.lease_ttl)
            if not claimed:
                self.robot_lists.pop(robot_id, None)
                return False
            manager.owner_key_value = token
            manager.lease_deadline = started + self.lease_ttl
            manager.start_task("owner", self._refresh_key(manager))
            return True

    async def _refresh_key(self, manager: RobotManager): #reviewed
        try:
            while not manager._closing:
                if manager.active_connections == 0 and manager.pending_offers == 0 and time.monotonic() - manager.last_activity > self.idle_timeout:
                    return
                started = time.monotonic()
                async with asyncio.timeout(3):
                    if not await self.redis.eval(REFRESH_IF_OWNER, 1, manager.owner_key, manager.owner_key_value, self.lease_ttl):
                        return
                    manager.lease_deadline = started + self.lease_ttl
                    if manager.sub_ready.is_set(): #manager owns a worker and is already listening for offer
                        if not await self.redis.eval(SET_IF_OWNER, 2, manager.owner_key, manager.announce_key, manager.owner_key_value, manager.owner_key_value, self.lease_ttl):
                            return
                await asyncio.sleep(self.lease_ttl / 3)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.exception(manager.logger_start + "Robot ownership renewal failed due to: " + str(error))
        finally:
            self._start_retire(manager)

    async def prepare_owner_session(self, robot_id: str, robot_role: str, offer_processor, wait_robot_ready: bool = False): #reviewed
        manager = self.get_robot_manager(robot_id)
        if manager is None or not await self.is_owner(manager):
            raise RuntimeError("This worker does not own the robot")
        async with manager.prepare_lock:
            listener = manager.tasks.get("listener")
            if listener is None:
                listener = manager.start_task("listener", self.listen_process_offer(robot_id, robot_role, offer_processor, manager.sub_ready))
            async with asyncio.timeout(5):
                while not manager.sub_ready.is_set():
                    if listener.done() or manager._closing:
                        raise RuntimeError("Robot offer listener failed to start")
                    await asyncio.sleep(0.02)
        if wait_robot_ready:
            robot = manager.get_robot_session(robot_role)
            if robot is None:
                raise RuntimeError("Robot role is offline")
            await robot.wait_for_video(timeout=10)

    async def _subscribe(self, pubsub, channel): #reviewed
        async with asyncio.timeout(5):
            await pubsub.subscribe(channel)
            while True:
                message = await pubsub.get_message(ignore_subscribe_messages=False, timeout=0.5)
                if message is not None and message["type"] == "subscribe" and message["channel"] == channel:
                    return

    async def listen_process_offer(self, robot_id: str, robot_role: str, offer_processor, sub_ready: asyncio.Event): #reviewed
        manager = self.get_robot_manager(robot_id)
        try:
            async with self.redis.pubsub() as pubsub:
                await self._subscribe(pubsub, manager.offer_channel)
                async with asyncio.timeout(3):
                    announced = await self.redis.eval(SET_IF_OWNER, 2, manager.owner_key, manager.announce_key, manager.owner_key_value, manager.owner_key_value, self.lease_ttl)
                if not announced:
                    raise RuntimeError("Ownership changed while subscribing")
                sub_ready.set()
                async for message in pubsub.listen():
                    if message["type"] != "message" or manager._closing:
                        continue
                    try:
                        data = json.loads(message["data"])
                        if not isinstance(data, dict) or data.get("type") != "offer":
                            continue
                        if data.get("robot_id") != robot_id or data.get("owner_token") != manager.owner_key_value:
                            continue
                        request_id = data.get("request_id")
                        self._validate_name(request_id, "request_id")
                        self._validate_name(data.get("robot_role"), "robot_role")
                        if data.get("reply_channel") != f"rtc:user:answer:{quote(robot_id, safe='')}:{request_id}":
                            continue
                        if sum(name.startswith("reply:") for name in manager.tasks) >= 256:
                            continue
                        manager.start_task(f"reply:{uuid.uuid4().hex}", self._reply_to_offer(manager, data, offer_processor))
                    except (ValueError, TypeError, KeyError) as error:
                        logger.warning(manager.logger_start + "Ignoring malformed RTC offer message, details: " + str(error))
        finally:
            sub_ready.clear()
            self._start_retire(manager)

    async def handle_usr_offer(self, robot_id, robot_role, offer, offer_processor, request_id): #reviewed
        self._validate_name(robot_role, "robot_role")
        self._validate_name(request_id, "request_id")
        offer_processor.validate_offer("ServerClient", offer)
        if self._closing or self.redis is None:
            raise RuntimeError("RTC service is unavailable")
        key_id = quote(robot_id, safe="")
        owner_key = f"rtc:user:owner:{key_id}"
        reply_channel = f"rtc:user:answer:{key_id}:{request_id}"
        async with asyncio.timeout(self.request_timeout):
            owner = await self.redis.get(owner_key)
            if owner is None:
                return {"type": "error", "error": "robot_not_ready", "retryable": True, "request_id": request_id}
            data = {"type": "offer", "role": "ServerClient", "robot_id": robot_id, "robot_role": robot_role, "request_id": request_id, "owner_token": owner, "reply_channel": reply_channel, "offer": offer}
            local = self.get_robot_manager(robot_id)
            #current worker owns the robot
            if local is not None and local.owner_key_value == owner and await self.is_owner(local):
                response, _ = await self._cached_offer(local, data, offer_processor)
            #other worker owns the robot, use redis to transfer offer
            else:
                async with self.redis.pubsub() as pubsub:
                    await self._subscribe(pubsub, reply_channel)
                    last_publish = 0
                    while True:
                        if self._closing or await self.redis.get(owner_key) != owner:
                            raise RuntimeError("Robot owner changed; retry with a new request_id")
                        if await self.redis.get(f"rtc:user:announce:{key_id}") != owner:
                            return {"type": "error", "error": "robot_not_ready", "retryable": True, "request_id": request_id}
                        if time.monotonic() - last_publish >= 1:
                            await self.redis.publish(f"rtc:user:offer:{key_id}", json.dumps(data))
                            last_publish = time.monotonic()
                        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.2)
                        if message is None:
                            continue
                        try:
                            response = json.loads(message["data"])
                            expected = {"request_id": request_id, "robot_id": robot_id, "robot_role": robot_role, "role": "ServerClient", "owner_token": owner}
                            if not isinstance(response, dict) or any(response.get(k) != v for k, v in expected.items()):
                                continue
                            if response.get("type") in ("answer", "error"):
                                break
                        except (ValueError, TypeError, KeyError) as error:
                            logger.warning(f"id: {robot_id} | role: {robot_role} | handle_user_offer() - " + str(error))
                            continue
            if await self.redis.get(owner_key) != owner:
                raise RuntimeError("Robot owner changed before answer delivery")
            if response.get("type") == "error":
                return {"type": "error", "error": response["error"], "retryable": response.get("retryable", True), "request_id": request_id}
            return {**response["answer"], "request_id": request_id, "session_id": response["session_id"], "owner_token": owner, "direct_control": True}

    async def _cached_offer(self, manager, data, offer_processor): #reviewed
        request_id = data["request_id"]
        signature = hashlib.sha256(json.dumps([data.get("role"), data["robot_role"], data.get("offer")], sort_keys=True).encode()).hexdigest()
        now = time.monotonic()
        manager.requests = {
            key: value 
            for key, value in manager.requests.items() 
            if not value[1].done() 
            or now - value[2] < self.request_cache_ttl
            }
        entry = manager.requests.get(request_id)
        if entry is not None and entry[0] != signature:
            return {"type": "error", "error": "request_id was reused for a different offer", "retryable": False}, None
        
        if entry is None:
            if len(manager.requests) >= 256:
                return {"type": "error", "error": "Too many recent offers", "retryable": True}, None
            manager.pending_offers += 1
            task = manager.start_task(f"offer:{request_id}", self._execute_offer(manager, data, offer_processor))
            entry = (signature, task, now)
            manager.requests[request_id] = entry
        response, session = await asyncio.shield(entry[1])

        if session is not None and session._closing:
            return {"type": "error", "error": "Session ended; use a new request_id", "retryable": True}, None
        
        if session is not None and isinstance(data.get("offer"), dict) and data["offer"].get("direct_control"):
            state = session.direct_pending
            if state is None or state["id"] != request_id or state.get("ended"):
                return {"type": "error", "error": "Negotiation ended; use a new request_id", "retryable": True}, None
        return response, session
    
    async def _reply_to_offer(self, manager, data, offer_processor): #reviewed
        response, session = await self._cached_offer(manager, data, offer_processor)
        response = {**response, "request_id": data["request_id"], "robot_id": manager.robot_id, "robot_role": data["robot_role"], "role": data.get("role"), "owner_token": manager.owner_key_value}
        try:
            async with asyncio.timeout(3):
                published = await self.redis.eval(PUBLISH_IF_OWNER, 2, manager.owner_key, data["reply_channel"], manager.owner_key_value, json.dumps(response))
            if not published:
                self._start_retire(manager)
        except Exception:
            self._start_retire(manager)
            raise

    async def _execute_offer(self, manager, data, offer_processor): #reviewed
        session = None
        direct = isinstance(data.get("offer"), dict) and data["offer"].get("direct_control") is True
        try:
            offer_processor.validate_offer(data.get("role"), data.get("offer"))
            if not await self.is_owner(manager):
                raise RuntimeError("Robot ownership changed")
            if direct:
                session = manager.get_robot_session(data["robot_role"])
                if session is None or not session.direct_ready():
                    raise RuntimeError("robot_not_ready")
                answer = await manager.relay_user_offer(data["robot_role"], data["offer"], data["request_id"], self.offer_timeout)
            else:
                session, answer = await offer_processor.process(data.get("role"), manager.robot_id, data["robot_role"], data.get("offer"))
            if not await self.is_owner(manager):
                raise RuntimeError("Robot ownership changed during negotiation")
            
            return {
                "type": "answer", 
                "answer": answer, 
                "session_id": getattr(session, "session_id", getattr(session, "client_id", None))
                }, session
        
        except asyncio.CancelledError:
            if session is not None and not direct: #direct session is handled by robot_manager
                await session.close()
            raise
        except Exception as exc:
            if session is not None and not direct:
                await session.close()
            logger.warning(manager.logger_start + "RTC offer failed due to: " + str(exc))

            return {
                "type": "error", 
                "error": str(exc) or type(exc).__name__, 
                "retryable": not isinstance(exc, ValueError)
                }, None
        
        finally:
            manager.pending_offers -= 1
            manager.last_activity = time.monotonic()
    
    async def watch_robot_ready(self, manager, robot): #reviewed
        try:
            while not robot._closing and manager.owns_lease():
                if robot.video_track is not None and robot.video_track.readyState == "live":
                    async with asyncio.timeout(3):
                        marked = await self.redis.eval(SET_IF_OWNER, 2, manager.owner_key, robot.ready_key, manager.owner_key_value, robot.ready_key_value, self.lease_ttl)
                    if not marked:
                        self._start_retire(manager)
                        return
                else:
                    await manager.clear_ready(robot)
                await asyncio.sleep(min(1, self.lease_ttl / 3))
        except asyncio.CancelledError:
            raise
        except Exception:
            self._start_retire(manager)
            raise
        finally:
            await manager.clear_ready(robot)

    @staticmethod
    def _validate_name(value, name): #reviewed
        if not isinstance(value, str) or not value or len(value) > 128 or any(not (c.isalnum() or c in "_-") for c in value):
            raise ValueError(f"Invalid {name}; use 1-128 letters, digits, underscores(_) or hyphens(-)")

    async def route_offer(self, peer_role, robot_id, robot_role, offer, offer_processor, request_id=None): #reviewed
        self._validate_name(robot_role, "robot_role")
        request_id = request_id or uuid.uuid4().hex
        self._validate_name(request_id, "request_id")
        offer_processor.validate_offer(peer_role, offer)
        if self._closing:
            raise RuntimeError("This worker is shutting down")
        key_id = quote(robot_id, safe="")
        owner_key = f"rtc:user:owner:{key_id}"
        announce_key = f"rtc:user:announce:{key_id}"
        channel = f"rtc:user:offer:{key_id}"
        reply_channel = f"rtc:user:answer:{key_id}:{request_id}"
        published_owner = None
        last_publish = 0
        async with asyncio.timeout(self.request_timeout):
            async with self.redis.pubsub() as pubsub:
                await self._subscribe(pubsub, reply_channel)
                while True:
                    if self._closing: #closing
                        raise RuntimeError("This worker is shutting down")
                    owner = await self.redis.get(owner_key)
                    if published_owner is not None and owner != published_owner: #the worker originally owning the robot id changed
                        raise RuntimeError("Owner changed; reconnect with a new offer and request_id")
                    local = self.get_robot_manager(robot_id)
                    if owner is None and peer_role == "MobileRobot": #no worker owns the robot id
                        if await self.acquire_worker(robot_id): #claiming ownership
                            await self.prepare_owner_session(robot_id, robot_role, offer_processor) #next stage
                        await asyncio.sleep(0.02)
                        continue
                    if owner is None: #non robot offer comes before robot offer
                        raise RuntimeError("Robot is offline")
                    if local is not None and local.owner_key_value == owner and not local._closing: #when non robot offer comes after robot connects
                        await self.prepare_owner_session(robot_id, robot_role, offer_processor) #next stage
                    if await self.redis.get(announce_key) == owner and time.monotonic() - last_publish >= 1: #when owner confirmed subing to redis, publish the recieved offer
                        data = {"type": "offer", "role": peer_role, "robot_id": robot_id, "robot_role": robot_role, "request_id": request_id, "owner_token": owner, "reply_channel": reply_channel, "offer": offer}
                        await self.redis.publish(channel, json.dumps(data))
                        published_owner = owner
                        last_publish = time.monotonic()
                    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.2) #await message
                    if message is None:
                        continue
                    try: #pass out the processed offer answer
                        response = json.loads(message["data"])
                        if not isinstance(response, dict):
                            continue
                        if any(response.get(key) != value for key, value in {"request_id": request_id, "robot_id": robot_id, "robot_role": robot_role, "role": peer_role, "owner_token": published_owner}.items()):
                            continue
                        if await self.redis.get(owner_key) != published_owner:
                            raise RuntimeError("Owner changed before answer delivery; reconnect")
                        if response.get("type") == "error":
                            raise RuntimeError(response.get("error", "RTC negotiation failed"))
                        if response.get("type") == "answer":
                            return {**response["answer"], "request_id": request_id, "session_id": response["session_id"], "owner_token": published_owner}
                    except (ValueError, TypeError, KeyError) as error:
                        logger.warning(f"id: {robot_id} | role: {robot_role} | router_offer() - Ignoring malformed RTC reply, details at: ", str(error))

    async def _wait_until_set(self, redis, key: str, timeout: float = 10): #reviewed
        async with asyncio.timeout(timeout):
            while not await redis.exists(key):
                await asyncio.sleep(0.1)

    def _start_retire(self, manager): #reviewed
        task = self._retire_tasks.get(manager.robot_id)
        if task is None:
            manager._closing = True
            manager.lease_deadline = 0
            task = asyncio.create_task(self._retire(manager))
            self._retire_tasks[manager.robot_id] = task
            task.add_done_callback(lambda done: self._retire_done(manager.robot_id, done))
        return task

    def _retire_done(self, robot_id, task): #reviewed
        if self._retire_tasks.get(robot_id) is task:
            self._retire_tasks.pop(robot_id, None)
        if not task.cancelled() and task.exception() is not None:
            logger.error(f"id: {robot_id} - Owner cleanup failed", exc_info=task.exception())

    async def _retire(self, manager): #reviewed
        try:
            await manager.cancel_tasks()
            await manager.close_sessions()
        finally:
            try:
                async with asyncio.timeout(3):
                    await self.redis.eval(DELETE_IF_OWNER, 1, manager.announce_key, manager.owner_key_value)
                    await self.redis.eval(DELETE_IF_OWNER, 1, manager.owner_key, manager.owner_key_value)
            except Exception:
                logger.exception("Could not release ownership; its TTL will expire")
            if self.robot_lists.get(manager.robot_id) is manager:
                self.robot_lists.pop(manager.robot_id, None)
            manager.requests.clear()
            manager.offer_locks.clear()

    async def close(self): #reviewed
        self._closing = True
        tasks = [self._start_retire(manager) for manager in list(self.robot_lists.values())]
        tasks.extend(self._retire_tasks.values())
        if tasks:
            await asyncio.shield(asyncio.gather(*set(tasks)))
