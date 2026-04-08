"""
FastAPI application factory.

All routes are registered in api/routes/.
Shared dependencies live in api/deps.py.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from api.routes.auth import router as auth_router
from api.routes.scan import router as scan_router
from config.main import get_settings

# ── Module-level DB handle (injected via api/deps.py:get_db) ─────────────────

_mongo_client: AsyncIOMotorClient | None = None
_db: AsyncIOMotorDatabase | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _mongo_client, _db
    settings = get_settings()
    _mongo_client = AsyncIOMotorClient(settings.mongo.uri.get_secret_value())
    _db = _mongo_client[settings.mongo.db]

    # Ensure indexes
    await _db["users"].create_index("email", unique=True)
    await _db["scan_jobs"].create_index([("user_id", -1), ("queued_at", -1)])
    await _db["scans"].create_index([("metadata.user_id", -1), ("metadata.completed_at", -1)])

    yield

    if _mongo_client:
        _mongo_client.close()


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="AWSVisualizer API", lifespan=lifespan)
app.include_router(auth_router)
app.include_router(scan_router)
