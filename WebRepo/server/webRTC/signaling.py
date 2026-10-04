import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager

import redis.asyncio as redis
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi_users.authentication import JWTStrategy

from server.auth import _authenticate_slave_api_key, COOKIE_NAME, JWT_SECRET, COOKIE_MAX_AGE
from server.models import User
from .webRTC_manager import WebRTCManager
from .offer_processor import RTCOfferProcessor

logger = logging.getLogger(__name__)
REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379")
_redis_client: redis.Redis | None = None
webrtc_manager: WebRTCManager | None = None
offer_processor: RTCOfferProcessor | None = None


@asynccontextmanager
async def rtc_lifespan(app): #reviewed
    global _redis_client, webrtc_manager, offer_processor
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


async def _ws_authenticate(websocket: WebSocket) -> User | None: #reviewed
    """Return the active User authenticated by API key or cookie JWT, otherwise None."""
    api_key = websocket.query_params.get("api_key")
    if api_key:
        try:
            from server.db import async_session_maker
            async with async_session_maker() as session:
                user = await _authenticate_slave_api_key(api_key, session)
                if user is not None and getattr(user, "is_active", False):
                    return user
        except Exception:
            logger.warning("RTC API-key authentication failed")
    token = websocket.cookies.get(COOKIE_NAME)
    if not token:
        return None
    try:
        from server.db import async_session_maker
        from server.auth import UserManager
        from fastapi_users_db_sqlalchemy import SQLAlchemyUserDatabase
        strategy = JWTStrategy(secret=JWT_SECRET, lifetime_seconds=COOKIE_MAX_AGE)
        async with async_session_maker() as session:
            user_db = SQLAlchemyUserDatabase(session, User)
            user = await strategy.read_token(token, UserManager(user_db))
            if user is not None and getattr(user, "is_active", False):
                return user
    except Exception:
        logger.warning("RTC cookie authentication failed")
    return None

async def route_usr_offer(user, robot_name, robot_role, offer, request_id): #reviewed
    if getattr(user, "id", None) is None:
        raise ValueError("User has no robot association")
    robot_id = f"{user.id}_{robot_name}"
    if offer.get("robot_name") != robot_name or offer.get("robot_role") != robot_role:
        raise ValueError("Offer robot_name and robot_role must match the authenticated route")
    if offer.get("robot_id", robot_id) != robot_id:
        raise ValueError("Offer robot_id does not belong to this user")
    return await webrtc_manager.handle_usr_offer(robot_id, robot_role, offer, offer_processor, request_id)

async def _handle_offer(websocket: WebSocket, user: User, robot_name: str, robot_role: str, peer_role: str): #reviewed
    if webrtc_manager is None or offer_processor is None:
        await websocket.close(code=1013, reason="RTC service is unavailable")
        return
    robot_id = f"{user.id}_{robot_name}"
    await websocket.accept()
    try:
        webrtc_manager._validate_name(robot_name, "robot_name")
        webrtc_manager._validate_name(robot_role, "robot_role")
        async with asyncio.timeout(10):
            offer = await websocket.receive_json()
        if not isinstance(offer, dict):
            raise ValueError("Expected a JSON SDP offer")
        if type(offer.get("direct_control", False)) is not bool:
            raise ValueError("direct_control must be a boolean")
        
        request_id = offer.get("request_id") or uuid.uuid4().hex
        if offer.get("direct_control"):
            if peer_role != "ServerClient":
                raise ValueError("Only clients may request direct control")
            answer = await route_usr_offer(user, robot_name, robot_role, offer, request_id)
        else:
            answer = await webrtc_manager.route_offer(peer_role, robot_id, robot_role, offer, offer_processor, request_id)
        await websocket.send_json(answer)
        await websocket.close(code=1000)
    except WebSocketDisconnect:
        logger.error(f"id: {robot_id} | role: {robot_role} | _handle_offer() - websocket disconnects")
        return
    except (ValueError, TimeoutError, RuntimeError) as exc:
        error = str(exc) or "RTC signaling timed out; reconnect with a new offer"
        logger.error(f"id: {robot_id} | role: {robot_role} | _handle_offer() - " + str(exc))
        try:
            await websocket.send_json({"type": "error", "error": error, "retryable": not isinstance(exc, ValueError)})
            await websocket.close(code=1013 if isinstance(exc, TimeoutError) else 1008)
        except (WebSocketDisconnect, RuntimeError):
            pass
    except Exception as exc:
        logger.error(f"id: {robot_id} | role: {robot_role} | _handle_offer() - " + str(exc))
        try:
            await websocket.send_json({"type": "error", "error": "RTC signaling failed; reconnect", "retryable": True})
            await websocket.close(code=1011)
        except (WebSocketDisconnect, RuntimeError):
            pass


@webRTC_router.websocket("/api/webrtc/robot/offer/{robot_name}/{robot_role}")
async def robot_offer(websocket: WebSocket, robot_name: str, robot_role: str): #reviewed
    api_key = websocket.query_params.get("api_key")
    user = None
    if api_key:
        try:
            from server.db import async_session_maker
            async with asyncio.timeout(10):
                async with async_session_maker() as session:
                    user = await _authenticate_slave_api_key(api_key, session)
        except Exception:
            logger.warning("Robot authentication failed")
    if user is None or not getattr(user, "is_active", False):
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
