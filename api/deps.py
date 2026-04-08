"""
Shared FastAPI dependencies injected into route handlers.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from motor.motor_asyncio import AsyncIOMotorDatabase

from config.main import get_settings

_bearer = HTTPBearer()


def get_db() -> AsyncIOMotorDatabase:
    from api.api import _db  # noqa: PLC0415  (import at call-time to avoid circular)
    assert _db is not None, "Database not initialised"
    return _db


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(_bearer)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> dict:
    settings = get_settings()
    try:
        payload = jwt.decode(
            credentials.credentials,
            settings.auth.jwt_secret.get_secret_value(),
            algorithms=[settings.auth.jwt_algorithm],
        )
        user_id: str | None = payload.get("sub")
        if not user_id:
            raise JWTError("missing sub")
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )

    user = await db["users"].find_one({"_id": user_id})
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user
