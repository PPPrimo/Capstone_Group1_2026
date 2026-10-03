from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import secrets
import sys
import time
from getpass import getpass
from pathlib import Path

from fastapi_users.exceptions import UserAlreadyExists
from sqlalchemy import delete, select


ROOT = Path(__file__).resolve().parent.parent
ROOT_STR = str(ROOT)
if ROOT_STR not in sys.path:
    sys.path.insert(0, ROOT_STR)

# Fail fast with a helpful error message if local imports won't work.
if not (ROOT / "server" / "__init__.py").exists():
    raise SystemExit(
        "Local package not found: expected 'server/__init__.py' under the project root. "
        f"Computed project root: {ROOT}"
    )
try:
    import server  # noqa: F401
except ModuleNotFoundError as exc:
    raise SystemExit(
        "Failed to import local package 'server'.\n"
        f"Computed project root: {ROOT}\n"
        "Try running from the repo root, or set PYTHONPATH to the repo root.\n"
        "Example (PowerShell): $env:PYTHONPATH=(Get-Location).Path; python .\\tools\\manage_users.py list"
    ) from exc

from server.db import create_db_and_tables, get_async_session  # noqa: E402
from server.models import User  # noqa: E402
from server.schemas import UserCreate  # noqa: E402
from server.auth import get_user_db, get_user_manager  # noqa: E402
from server.db import close_db  # noqa: E402


get_async_session_context = contextlib.asynccontextmanager(get_async_session)
get_user_db_context = contextlib.asynccontextmanager(get_user_db)
get_user_manager_context = contextlib.asynccontextmanager(get_user_manager)


async def _run_with_cleanup(coro) -> None:
    try:
        await coro
    finally:
        # Important on Windows: ensure aiosqlite background threads exit.
        await close_db()


async def list_users() -> None:
    await create_db_and_tables()
    async with get_async_session_context() as session:
        rows = (await session.execute(select(User.email).order_by(User.email))).all()
        if not rows:
            print("No users configured.")
            return
        for (email,) in rows:
            print(email)


async def add_user(email: str, password: str, is_superuser: bool, force: bool) -> None:
    await create_db_and_tables()
    async with get_async_session_context() as session:
        async with get_user_db_context(session) as user_db:
            async with get_user_manager_context(user_db) as user_manager:
                try:
                    user = await user_manager.create(
                        UserCreate(email=email, password=password, is_superuser=is_superuser)
                    )
                    print(f"User created: {user.email} (superuser={user.is_superuser})")
                    return
                except UserAlreadyExists:
                    if not force:
                        raise SystemExit(f"User already exists: {email} (use --force to overwrite)")

                # Overwrite by removing then recreating.
                await session.execute(delete(User).where(User.email == email))
                await session.commit()
                user = await user_manager.create(
                    UserCreate(email=email, password=password, is_superuser=is_superuser)
                )
                print(f"User overwritten: {user.email} (superuser={user.is_superuser})")


async def remove_user(email: str) -> None:
    await create_db_and_tables()
    async with get_async_session_context() as session:
        res = await session.execute(delete(User).where(User.email == email))
        await session.commit()
        if res.rowcount and res.rowcount > 0:
            print(f"Removed user: {email}")
        else:
            raise SystemExit(f"User not found: {email}")


async def generate_api_key(email: str) -> None:
    """Generate/rotate a per-user API key.

    Stores only a SHA-256 hash in the DB; the full key is displayed once.
    """
    await create_db_and_tables()
    async with get_async_session_context() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if not user:
            raise SystemExit(f"User not found: {email}")

        prefix = "uapi_" + secrets.token_hex(5)  # 15 chars total
        secret = secrets.token_urlsafe(32)
        api_key = f"{prefix}.{secret}"

        user.api_key_prefix = prefix
        user.api_key_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
        user.api_key_created_at = time.time()
        session.add(user)
        await session.commit()

        print(f"API key rotated for: {email}")
        print("Store this value securely; it will not be shown again:")
        print(api_key)

def cmd_list(_: argparse.Namespace) -> None:
    asyncio.run(_run_with_cleanup(list_users()))


def cmd_add(args: argparse.Namespace) -> None:
    email = args.email.strip()
    if not email:
        raise SystemExit("Email cannot be empty")

    password = args.password
    if password is None:
        password = getpass(f"Password for {email}: ")
        confirm = getpass("Confirm password: ")
        if password != confirm:
            raise SystemExit("Passwords do not match")
    if not password:
        raise SystemExit("Password cannot be empty")

    is_admin = bool(getattr(args, "admin", False) or getattr(args, "superuser", False))
    asyncio.run(_run_with_cleanup(add_user(email, password, is_admin, args.force)))


def cmd_remove(args: argparse.Namespace) -> None:
    email = args.email.strip()
    if not email:
        raise SystemExit("Email cannot be empty")
    asyncio.run(_run_with_cleanup(remove_user(email)))


def cmd_apikey(args: argparse.Namespace) -> None:
    email = args.email.strip()
    if not email:
        raise SystemExit("Email cannot be empty")
    asyncio.run(_run_with_cleanup(generate_api_key(email)))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Manage FastAPI-Users accounts stored in server/sensitive/users.db (gitignored)."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="List configured user emails")
    p_list.set_defaults(func=cmd_list)

    p_add = sub.add_parser("add", help="Add (or update) a user")
    p_add.add_argument("email", help="User email (used as login username)")
    p_add.add_argument("--password", help="Password (omit to be prompted)")
    p_add.add_argument(
        "--admin",
        action="store_true",
        help="Mark as admin account (alias for --superuser)",
    )
    p_add.add_argument(
        "--superuser",
        action="store_true",
        help="Mark as admin/superuser (deprecated; use --admin)",
    )
    p_add.add_argument("--force", action="store_true", help="Overwrite if user exists")
    p_add.set_defaults(func=cmd_add)

    p_rm = sub.add_parser("remove", help="Remove a user")
    p_rm.add_argument("email")
    p_rm.set_defaults(func=cmd_remove)

    p_key = sub.add_parser("apikey", help="Generate/rotate a per-user API key")
    p_key.add_argument("email", help="User email (API key will be tied to this account)")
    p_key.set_defaults(func=cmd_apikey)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
