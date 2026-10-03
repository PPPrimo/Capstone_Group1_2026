import os
import uuid
import hashlib
import hmac

from fastapi import APIRouter, Depends, Request
from fastapi_users import BaseUserManager, FastAPIUsers, UUIDIDMixin
from fastapi_users.authentication import (
    AuthenticationBackend,
    CookieTransport,
    JWTStrategy,
)
from fastapi_users_db_sqlalchemy import SQLAlchemyUserDatabase

from server.db import get_async_session
from server.models import User

from sqlalchemy import select



APP_ENV = os.getenv("APP_ENV", "development").lower()
JWT_SECRET = os.getenv("AUTH_JWT_SECRET", "CHANGE_ME_IN_PROD")
if APP_ENV in ("prod", "production") and JWT_SECRET == "CHANGE_ME_IN_PROD":
    raise RuntimeError("AUTH_JWT_SECRET must be set in production (do not use the default placeholder).")

# Cookie-based auth is intended for browser usage.
# Keep Secure cookies in prod; allow override for local HTTP if needed.
COOKIE_SECURE = os.getenv("AUTH_COOKIE_SECURE", "true").lower() not in ("0", "false", "no")
COOKIE_NAME = os.getenv("AUTH_COOKIE_NAME", "fastapiusersauth")
COOKIE_MAX_AGE = int(os.getenv("AUTH_COOKIE_MAX_AGE", str(7 * 24 * 60 * 60)))

class UserManager(UUIDIDMixin, BaseUserManager[User, uuid.UUID]):
    reset_password_token_secret = JWT_SECRET
    verification_token_secret = JWT_SECRET

#User data storage
async def get_user_db(session=Depends(get_async_session)):
    yield SQLAlchemyUserDatabase(session, User)

async def get_user_manager(user_db=Depends(get_user_db)):
    yield UserManager(user_db)

def get_jwt_strategy() -> JWTStrategy:
    return JWTStrategy(secret=JWT_SECRET, lifetime_seconds=COOKIE_MAX_AGE)

cookie_transport = CookieTransport(
    cookie_name=COOKIE_NAME,
    cookie_max_age=COOKIE_MAX_AGE,
    cookie_secure=COOKIE_SECURE,
    cookie_httponly=True,
    cookie_samesite="lax",
)
auth_backend = AuthenticationBackend(
    name="cookie-jwt",
    transport=cookie_transport,
    get_strategy=get_jwt_strategy,
)
fastapi_users = FastAPIUsers[User, uuid.UUID](get_user_manager, [auth_backend])
current_active_user = fastapi_users.current_user(active=True)
current_optional_user = fastapi_users.current_user(optional=True)
current_superuser = fastapi_users.current_user(active=True, superuser=True)

#user authentication
async def _authenticate_slave_api_key(api_key: str | None, session) -> User | None:
    if not api_key:
        return None
    if "." not in api_key:
        return None
    prefix = api_key.split(".", 1)[0]
    if not prefix:
        return None

    user = (await session.execute(select(User).where(User.api_key_prefix == prefix))).scalar_one_or_none()
    if not user or not user.api_key_hash:
        return None

    digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    if hmac.compare_digest(digest, user.api_key_hash):
        return user
    return None

async def _require_active_user_or_api_key(
    request: Request,
    session=Depends(get_async_session),
    browser_user: User | None = Depends(current_optional_user),
) -> User:
    """Authorize either a logged-in browser user (cookie JWT) or an API key client."""

    if browser_user is not None and getattr(browser_user, "is_active", False):
        return browser_user

    api_key = request.headers.get("x-api-key")
    api_user = await _authenticate_slave_api_key(api_key, session)
    if api_user is not None and getattr(api_user, "is_active", False):
        return api_user

    raise HTTPException(status_code=401, detail="Unauthorized")
