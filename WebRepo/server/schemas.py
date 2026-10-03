import uuid

from fastapi_users import schemas


class UserRead(schemas.BaseUser[uuid.UUID]):
    pass


class UserCreate(schemas.BaseUserCreate):
    # FastAPI-Users uses email as the unique identifier.
    # We treat "email" as the login username in the UI.
    pass


class UserUpdate(schemas.BaseUserUpdate):
    pass
