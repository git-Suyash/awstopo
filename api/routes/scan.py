"""
Scan management routes.

  POST /api/scan/start           — trigger a full resource scan for the caller's AWS account
  GET  /api/scan/{scan_id}       — poll scan status
  GET  /api/scan                 — list all scans for the caller (newest first)
  GET  /api/graph                — return full graph (nodes + edges) for the caller
  GET  /api/graph/{scan_id}      — return graph for a specific past scan
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from api.deps import get_current_user, get_db
from config.main import get_settings

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api", tags=["scan"])

# ── Background scan worker ────────────────────────────────────────────────────

async def _run_scan_background(
    scan_id: str,
    user_id: str,
    role_arn: str,
    external_id: str,
    regions: list[str],
) -> None:
    """
    Runs the full pipeline in the background.
    Opens its own DB connections so it is independent of the request lifecycle.
    Updates the scan_jobs document throughout.
    """
    # Import here to avoid circular imports at module load time
    from main import run_scan  # noqa: PLC0415

    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongo.uri.get_secret_value())
    db = client[settings.mongo.db]

    try:
        await db["scan_jobs"].update_one(
            {"_id": scan_id},
            {"$set": {"status": "running", "started_running_at": datetime.now(timezone.utc)}},
        )

        graph_doc = await run_scan(
            user_id=user_id,
            role_arn=role_arn,
            external_id=external_id,
            regions=regions or None,
            settings=settings,
        )

        await db["scan_jobs"].update_one(
            {"_id": scan_id},
            {
                "$set": {
                    "status": "completed",
                    "completed_at": datetime.now(timezone.utc),
                    "node_count": graph_doc.metadata.node_count,
                    "edge_count": graph_doc.metadata.edge_count,
                    "errors": list(graph_doc.metadata.collector_errors.keys()) or None,
                }
            },
        )
        log.info("scan.background.done", scan_id=scan_id, user_id=user_id)

    except Exception as exc:
        log.exception("scan.background.failed", scan_id=scan_id, error=str(exc))
        await db["scan_jobs"].update_one(
            {"_id": scan_id},
            {
                "$set": {
                    "status": "failed",
                    "failed_at": datetime.now(timezone.utc),
                    "error": str(exc),
                }
            },
        )
    finally:
        client.close()


def _strip_mongo_id(doc: dict[str, Any]) -> dict[str, Any]:
    """Replace MongoDB _id with scan_id for clean API responses."""
    doc = dict(doc)
    if "_id" in doc:
        doc["scan_id"] = str(doc.pop("_id"))
    return doc


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/scan/start", status_code=status.HTTP_202_ACCEPTED)
async def start_scan(
    background_tasks: BackgroundTasks,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
):
    """
    Trigger a full AWS resource scan for the authenticated user.

    The scan runs asynchronously. Poll GET /api/scan/{scan_id} for status.
    Returns immediately with a scan_id.
    """
    user_id = current_user["_id"]

    account = await db["accounts"].find_one({"user_id": user_id})
    if not account:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No AWS account configured. Call POST /api/user/configureaws first.",
        )

    scan_id = str(uuid.uuid4())
    await db["scan_jobs"].insert_one({
        "_id": scan_id,
        "user_id": user_id,
        "status": "queued",
        "queued_at": datetime.now(timezone.utc),
    })

    settings = get_settings()
    background_tasks.add_task(
        _run_scan_background,
        scan_id=scan_id,
        user_id=user_id,
        role_arn=account["role_arn"],
        external_id=account["external_id"],
        regions=settings.aws.regions,
    )

    return {"scan_id": scan_id, "status": "queued"}


@router.get("/scan/{scan_id}")
async def get_scan_status(
    scan_id: str,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
):
    """Return the status and summary of a specific scan."""
    job = await db["scan_jobs"].find_one(
        {"_id": scan_id, "user_id": current_user["_id"]}
    )
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scan not found")
    return _strip_mongo_id(job)


@router.get("/scan")
async def list_scans(
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
):
    """List all scans for the authenticated user, newest first."""
    cursor = (
        db["scan_jobs"]
        .find({"user_id": current_user["_id"]})
        .sort("queued_at", -1)
        .limit(50)
    )
    jobs = await cursor.to_list(length=50)
    return [_strip_mongo_id(j) for j in jobs]


@router.get("/graph")
async def get_graph(
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
):
    """
    Return the full resource graph (nodes, edges, groups) for the most recent
    completed scan belonging to the authenticated user.
    """
    user_id = current_user["_id"]

    doc = await db["scans"].find_one(
        {"metadata.user_id": user_id},
        sort=[("metadata.completed_at", -1)],
    )
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No completed scan found. Run POST /api/scan/start first.",
        )

    doc.pop("_id", None)
    return doc


@router.get("/graph/{scan_id}")
async def get_graph_by_scan(
    scan_id: str,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
):
    """Return the resource graph for a specific past scan."""
    doc = await db["scans"].find_one(
        {"_id": scan_id, "metadata.user_id": current_user["_id"]}
    )
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scan not found")
    doc.pop("_id", None)
    return doc
