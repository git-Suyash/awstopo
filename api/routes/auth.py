"""
Authentication routes.

  POST /api/user/signup        — register with email + password, returns JWT
  POST /api/user/login         — authenticate, returns JWT
  GET  /api/user/externalid    — return the caller's unique ExternalId (JWT-gated)
  POST /api/user/configureaws  — store an AWS role ARN for the caller (JWT-gated)
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

import bcrypt as _bcrypt
from fastapi import APIRouter, Depends, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from jose import jwt
from pydantic import BaseModel, EmailStr

from api.deps import get_current_user, get_db
from config.main import get_settings

router = APIRouter(prefix="/api/user", tags=["auth"])


# ── Password helpers ──────────────────────────────────────────────────────────

def _hash_password(password: str) -> str:
    digest = hashlib.sha256(password.encode()).hexdigest().encode()
    return _bcrypt.hashpw(digest, _bcrypt.gensalt()).decode()


def _verify_password(password: str, hashed: str) -> bool:
    digest = hashlib.sha256(password.encode()).hexdigest().encode()
    return _bcrypt.checkpw(digest, hashed.encode())


def _create_token(user_id: str) -> str:
    settings = get_settings()
    expiry = datetime.now(timezone.utc) + timedelta(minutes=settings.auth.jwt_expiry_minutes)
    return jwt.encode(
        {"sub": user_id, "exp": expiry},
        settings.auth.jwt_secret.get_secret_value(),
        algorithm=settings.auth.jwt_algorithm,
    )


# ── Request / response models ─────────────────────────────────────────────────

class SignupRequest(BaseModel):
    email: EmailStr
    password: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class ConfigureAWSRequest(BaseModel):
    role_arn: str


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/signup", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def signup(
    body: SignupRequest,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
):
    """Register a new user. Returns a JWT on success."""
    if await db["users"].find_one({"email": body.email}):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    user_id = str(uuid.uuid4())
    await db["users"].insert_one({
        "_id": user_id,
        "email": body.email,
        "hashed_password": _hash_password(body.password),
        "external_id": str(uuid.uuid4()),  # unique per-user for IAM trust policy ExternalId
        "created_at": datetime.now(timezone.utc),
    })
    return TokenResponse(access_token=_create_token(user_id))


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
):
    """Authenticate with email + password. Returns a JWT on success."""
    user = await db["users"].find_one({"email": body.email})
    if not user or not _verify_password(body.password, user["hashed_password"]):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    return TokenResponse(access_token=_create_token(user["_id"]))


@router.get("/externalid")
async def get_external_id(
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Return the caller's unique ExternalId for use in their IAM role trust policy."""
    return {"external_id": current_user["external_id"]}


@router.post("/configureaws", status_code=status.HTTP_200_OK)
async def configure_aws(
    body: ConfigureAWSRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
):
    """
    Store an AWS IAM role ARN for the authenticated user.

    The role's trust policy must allow this service to assume it and must
    include: Condition: { StringEquals: { 'sts:ExternalId': <your external_id> } }
    """
    if not body.role_arn.startswith("arn:aws"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="role_arn must be a valid AWS ARN starting with 'arn:aws'",
        )

    await db["accounts"].replace_one(
        {"user_id": current_user["_id"]},
        {
            "user_id": current_user["_id"],
            "role_arn": body.role_arn,
            "external_id": current_user["external_id"],
            "configured_at": datetime.now(timezone.utc),
        },
        upsert=True,
    )
    return {
        "role_arn": body.role_arn,
        "external_id": current_user["external_id"],
        "message": "AWS account configured. Ensure your IAM role trust policy specifies this ExternalId.",
    }
