import asyncio
import hashlib
import hmac
import json
import os
import time
from pathlib import Path
import traceback
import logging

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from starlette.staticfiles import StaticFiles

from sqlalchemy import select

#User defined functions
from server.auth import auth_backend, cookie_transport, current_active_user, current_optional_user, current_superuser, fastapi_users
from server.db import create_db_and_tables, get_async_session
from server.models import User
from server.server_status import server_status_router, stats_collector_task, persist_stats_history, stats_startup, stats_shutdown
# from server.webRTC.signaling import webRTC_router

WORKSPACE_DIR = Path(__file__).resolve().parent.parent
PUBLIC_DIR = WORKSPACE_DIR / "public"
logging.basicConfig(filename="server/sensitive/log/server.log", level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI()
app.include_router(server_status_router)
app.include_router(webRTC_router)

_collector_task: asyncio.Task | None = None

APP_ENV = os.getenv("APP_ENV", "development").lower()


@app.on_event("startup")
async def on_startup():
    global _collector_task
    await create_db_and_tables()
    await stats_startup()
    _collector_task = asyncio.create_task(stats_collector_task())

@app.on_event("shutdown")
async def on_shutdown():
    global _collector_task
    if _collector_task:
        _collector_task.cancel()
        try:
            await _collector_task
        except asyncio.CancelledError:
            pass
    await persist_stats_history()
    await stats_shutdown()

# Framework-managed auth routes:
# - POST /auth/jwt/login
# - POST /auth/jwt/logout
app.include_router(
    fastapi_users.get_auth_router(auth_backend),
    prefix="/auth/jwt",
    tags=["auth"],
)

@app.exception_handler(Exception)
async def debug_exception_handler(request: Request, exc: Exception):
    tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
    return JSONResponse(
        status_code=500,
        content={"detail": "".join(tb)},
    )

@app.middleware("http")
async def security_headers_and_methods(request: Request, call_next):
    # Block access to internal directories (defense-in-depth)
    path = request.url.path
    if path.startswith(("/server/", "/tools/", "/dev_local/")):
        return PlainTextResponse("Forbidden", status_code=403)

    # Allow only safe methods globally; allow POST for login/logout and slave ingest;
    # allow DELETE for admin history clear.
    if request.method not in ("GET", "HEAD"):
        if request.method == "POST" and path in ("/auth/jwt/login", "/auth/jwt/logout"):
            pass
        elif request.method == "DELETE" and path == "/api/system-history":
            pass
        else:
            return PlainTextResponse("Method Not Allowed", status_code=405)

    response: Response = await call_next(request)

    # Best-effort: discourage caching and reduce accidental downloads
    response.headers["Content-Disposition"] = "inline"
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    return response

# ── /api/me  ──  let the browser discover the current user's role ──
@app.get("/api/me")
async def me(user: User = Depends(current_active_user)):
    return {
        "email": user.email,
        "is_superuser": user.is_superuser,
    }

@app.get("/api/logout")
async def logout():
    # Convenience GET endpoint for the UI; FastAPI-Users official logout is POST /auth/jwt/logout.
    resp = RedirectResponse("/login.html", status_code=302)
    resp.delete_cookie(cookie_transport.cookie_name)
    return resp

@app.get("/login.html")
async def login_page():
    return FileResponse(PUBLIC_DIR / "login.html")

@app.get("/")
async def gate_root(user=Depends(current_optional_user)):
    if not user:
        return RedirectResponse("/login.html", status_code=302)
    return FileResponse(PUBLIC_DIR / "index.html")

@app.get("/index.html")
async def gate_index(user=Depends(current_optional_user)):
    if not user:
        return RedirectResponse("/login.html", status_code=302)
    return FileResponse(PUBLIC_DIR / "index.html")

@app.get("/feature1.html")
async def gate_f1(user=Depends(current_optional_user)):
    if not user:
        return RedirectResponse("/login.html", status_code=302)
    return FileResponse(PUBLIC_DIR / "feature1.html")

@app.get("/feature2.html")
async def gate_f2(user=Depends(current_optional_user)):
    if not user:
        return RedirectResponse("/login.html", status_code=302)
    return FileResponse(PUBLIC_DIR / "feature2.html")

@app.get("/feature3.html")
async def gate_f3(user=Depends(current_optional_user)):
    if not user:
        return RedirectResponse("/login.html", status_code=302)
    return FileResponse(PUBLIC_DIR / "feature3.html")

# Serve the entire website directory as static content
app.mount(
    "/",
    StaticFiles(directory=str(PUBLIC_DIR), html=True),
    name="static",
)

