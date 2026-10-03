import os
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

WORKSPACE_DIR = Path(__file__).resolve().parent.parent
SENSITIVE_DIR = WORKSPACE_DIR / "server" / "sensitive"
SENSITIVE_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = SENSITIVE_DIR / "users.db"
DATABASE_URL = os.getenv("AUTH_DATABASE_URL", f"sqlite+aiosqlite:///{DB_PATH.as_posix()}")

engine = create_async_engine(DATABASE_URL, echo=False)
async_session_maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def _apply_sqlite_migrations(conn) -> None:
    # Lightweight, idempotent migrations for the local SQLite auth DB.
    # We avoid Alembic here on purpose to keep the project minimal.
    if not DATABASE_URL.startswith("sqlite"):
        return

    # FastAPI-Users SQLAlchemy table name is typically "user".
    rows = (await conn.exec_driver_sql('PRAGMA table_info("user")')).all()
    if not rows:
        return

    existing_cols = {r[1] for r in rows}
    desired = {
        "api_key_prefix": "VARCHAR(16)",
        "api_key_hash": "VARCHAR(255)",
        "api_key_created_at": "FLOAT",
    }
    for col_name, col_type in desired.items():
        if col_name not in existing_cols:
            await conn.exec_driver_sql(f'ALTER TABLE "user" ADD COLUMN {col_name} {col_type}')

    # Optional index to speed up prefix lookup.
    await conn.exec_driver_sql('CREATE INDEX IF NOT EXISTS ix_user_api_key_prefix ON "user" (api_key_prefix)')


async def create_db_and_tables() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _apply_sqlite_migrations(conn)


async def get_async_session():
    async with async_session_maker() as session:
        yield session

async def close_db():
    await engine.dispose()
