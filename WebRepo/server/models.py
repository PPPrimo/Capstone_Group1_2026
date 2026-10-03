from sqlalchemy import Float, String
from sqlalchemy.orm import Mapped, mapped_column

from fastapi_users_db_sqlalchemy import SQLAlchemyBaseUserTableUUID

from server.db import Base


class User(SQLAlchemyBaseUserTableUUID, Base):
    api_key_prefix: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    api_key_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    api_key_created_at: Mapped[float | None] = mapped_column(Float, nullable=True)
