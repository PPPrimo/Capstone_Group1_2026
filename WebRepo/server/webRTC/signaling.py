import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import redis.asyncio as redis
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi_users.authentication import JWTStrategy

from server.auth import _authenticate_slave_api_key, _require_active_user_or_api_key, current_active_user, COOKIE_NAME, JWT_SECRET, COOKIE_MAX_AGE
from server.models import User
from server.db import async_session_maker
from server.auth import UserManager
from fastapi_users_db_sqlalchemy import SQLAlchemyUserDatabase
from .webRTC_manager import WebRTCManager
from .offer_processor import RTCOfferProcessor
from .ice_config import ice_credentials, turn_settings

logger = logging.getLogger(__name__)
REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379")
_redis_client: redis.Redis | None = None
webrtc_manager: WebRTCManager | None = None
offer_processor: RTCOfferProcessor | None = None


@asynccontextmanager
async def rtc_lifespan(app): #reviewed
    global _redis_client, webrtc_manager, offer_processor
    turn_settings()
    # Construct per-worker resources after process startup, including pre-fork servers.
    _redis_client = redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=3, socket_timeout=None)
    webrtc_manager = WebRTCManager(worker_id=f"{os.getpid()}:{uuid.uuid4().hex}", redis=_redis_client)
    offer_processor = RTCOfferProcessor(webrtc_manager)
    try:
        async with asyncio.timeout(3):
            await _redis_client.ping()
        yield
    finally:
        try:
            await webrtc_manager.close()
        finally:
            await _redis_client.aclose()
            offer_processor = None
            webrtc_manager = None
            _redis_client = None


webRTC_router = APIRouter(lifespan=rtc_lifespan)


@webRTC_router.get("/api/webrtc/ice-servers")
async def get_ice_servers(user: User = Depends(_require_active_user_or_api_key)):
    # Redis keeps the limit shared across all gunicorn workers.
    if _redis_client is None:
        raise HTTPException(status_code=503, detail="RTC service is unavailable")
    try:
        count = await _redis_client.eval(
            "local n = redis.call('INCR', KEYS[1]); "
            "if n == 1 then redis.call('EXPIRE', KEYS[1], 60) end; return n",
            1, f"rtc:ice-credentials:{user.id}",
        )
    except redis.RedisError:
        raise HTTPException(status_code=503, detail="RTC service is unavailable") from None
    if count > 30:
        raise HTTPException(status_code=429, detail="Too many credential requests",
                            headers={"Retry-After": "60", "Cache-Control": "no-store"})
    try:
        payload = ice_credentials(str(user.id))
    except (KeyError, ValueError):
        raise HTTPException(status_code=503, detail="TURN configuration is invalid") from None
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@webRTC_router.get("/api/webrtc/live_webrtc.js")
async def client_module(user: User = Depends(current_active_user)):
    return FileResponse(Path(__file__).with_name("live_webrtc.js"), media_type="text/javascript",
                        headers={"Cache-Control": "no-store"})


async def _ws_authenticate(websocket: WebSocket, robot=False, cookie_only=False) -> User | None:
    async with async_session_maker() as session:
        api_key = None if cookie_only else websocket.query_params.get("api_key")
        user = await _authenticate_slave_api_key(api_key, session)
        token = websocket.cookies.get(COOKIE_NAME)
        if user is None and not robot and token:
            user_db = SQLAlchemyUserDatabase(session, User)
            strategy = JWTStrategy(secret=JWT_SECRET, lifetime_seconds=COOKIE_MAX_AGE)
            user = await strategy.read_token(token, UserManager(user_db))
        return user if user is not None and user.is_active else None


@webRTC_router.websocket("/api/webrtc/robots")
async def robot_status(websocket: WebSocket):
    try:
        async with asyncio.timeout(10):
            user = await _ws_authenticate(websocket, cookie_only=True)
    except TimeoutError:
        user = None
    if user is None:
        await websocket.close(code=4401, reason="Unauthorized")
        return
    if webrtc_manager is None:
        await websocket.close(code=1013, reason="RTC service is unavailable")
        return
    await websocket.accept()
    try:
        async with _redis_client.pubsub() as pubsub:
            # Subscribe first, so changes during the initial worker lookup are queued.
            await webrtc_manager._subscribe(pubsub, f"rtc:user:status:{user.id}")

            async def send_status():
                robots = await webrtc_manager.user_robot_status(str(user.id))
                states = {(robot["robot_name"], robot["robot_role"]): robot for robot in robots}

                async def send_snapshot():
                    await websocket.send_json({"type": "robot_status", "robots":
                                               [states[key] for key in sorted(states)]})

                await send_snapshot()
                async for message in pubsub.listen():
                    if message["type"] == "message":
                        robot = json.loads(message["data"])
                        states[robot["robot_name"], robot["robot_role"]] = robot
                        await send_snapshot()

            async def wait_for_disconnect():
                async for _ in websocket.iter_text():
                    pass

            tasks = [asyncio.create_task(send_status()), asyncio.create_task(wait_for_disconnect())]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    except WebSocketDisconnect:
        pass
    except (redis.RedisError, TimeoutError):
        logger.exception("Robot status subscription failed for user %s", user.id)
        await websocket.close(code=1013, reason="Robot status is temporarily unavailable")


async def _handle_offer(websocket, user, robot_name, robot_role, peer_role):
    if webrtc_manager is None or offer_processor is None:
        await websocket.close(code=1013, reason="RTC service is unavailable")
        return
    robot_id = f"{user.id}_{robot_name}"
    await websocket.accept()
    try:
        try:
            webrtc_manager._validate_name(robot_name, "robot_name")
            async with asyncio.timeout(10):
                offer = await websocket.receive_json()
            if not isinstance(offer, dict):
                raise ValueError("Expected an SDP offer")
            expected = {"robot_name": robot_name, "robot_role": robot_role, "robot_id": robot_id}
            if any(offer.get(key, value) != value for key, value in expected.items()):
                raise ValueError("Offer does not match the authenticated route")
            answer = await webrtc_manager.route_offer(peer_role, robot_id, robot_role, offer,
                                                      offer_processor, offer.get("request_id"))
        except (ValueError, ConnectionError, TimeoutError, redis.RedisError) as error:
            answer = {"type": "error", "error": str(error) or "RTC signaling timed out",
                      "retryable": not isinstance(error, ValueError)}
        await websocket.send_json(answer)
        await websocket.close(code=1000)
    except WebSocketDisconnect:
        logger.info("RTC signaling disconnected for %s", robot_id)


@webRTC_router.websocket("/api/webrtc/robot/offer/{robot_name}/{robot_role}")
async def robot_offer(websocket: WebSocket, robot_name: str, robot_role: str): #reviewed
    try:
        async with asyncio.timeout(10):
            user = await _ws_authenticate(websocket, robot=True)
    except TimeoutError:
        user = None
    if user is None:
        await websocket.close(code=4401, reason="Unauthorized")
        return
    await _handle_offer(websocket, user, robot_name, robot_role, "MobileRobot")


@webRTC_router.websocket("/api/webrtc/client/offer/{robot_name}/{robot_role}")
async def client_offer(websocket: WebSocket, robot_name: str, robot_role: str): #use case does not exist
    try:
        async with asyncio.timeout(10):
            user = await _ws_authenticate(websocket)
    except TimeoutError:
        user = None
    if user is None:
        await websocket.close(code=4401, reason="Unauthorized")
        return
    await _handle_offer(websocket, user, robot_name, robot_role, "ServerClient")
