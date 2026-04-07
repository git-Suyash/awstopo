"""
Async MongoDB storage via Motor.

Collections:
  scans        — one document per scan run (GraphDocument)
  scan_index   — lightweight index: {scan_id, account_id, started_at, node_count, edge_count}
"""

from __future__ import annotations

from typing import Any

import structlog
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import DESCENDING, IndexModel

from config.main import MongoSettings
from exceptions.main import StorageError
from graph.graph import GraphDocument



log = structlog.get_logger(__name__)

_SCANS_COLLECTION = "scans"
_INDEX_COLLECTION = "scan_index"


class MongoStore:
    def __init__(self, settings: MongoSettings) -> None:
        self._settings = settings
        self._client: AsyncIOMotorClient | None = None
        self._db: AsyncIOMotorDatabase | None = None

    async def connect(self) -> None:
        kwargs: dict[str, Any] = {
            "serverSelectionTimeoutMS": 5000,
        }
        if self._settings.tls:
            kwargs["tls"] = True
            if self._settings.tls_ca_file:
                kwargs["tlsCAFile"] = self._settings.tls_ca_file

        self._client = AsyncIOMotorClient(
            self._settings.uri.get_secret_value(), **kwargs
        )
        self._db = self._client[self._settings.db]
        await self._ensure_indexes()
        log.info("mongo.connected", db=self._settings.db)

    async def disconnect(self) -> None:
        if self._client:
            self._client.close()
            log.info("mongo.disconnected")

    async def _ensure_indexes(self) -> None:
        assert self._db is not None  # noqa: S101
        await self._db[_SCANS_COLLECTION].create_indexes(
            [
                IndexModel([("metadata.account_id", DESCENDING)]),
                IndexModel([("metadata.started_at", DESCENDING)]),
                IndexModel([("metadata.scan_id", DESCENDING)], unique=True),
            ]
        )
        await self._db[_INDEX_COLLECTION].create_indexes(
            [
                IndexModel([("account_id", DESCENDING), ("started_at", DESCENDING)]),
            ]
        )

    async def save_scan(self, doc: GraphDocument) -> str:
        """
        Upsert a GraphDocument into the scans collection.

        Returns the scan_id used as the document _id.
        Idempotent: re-running with the same scan_id overwrites.
        """
        assert self._db is not None  # noqa: S101
        scan_id = doc.metadata.scan_id
        try:
            collection = self._db[_SCANS_COLLECTION]
            raw = doc.to_mongo_dict()
            await collection.replace_one({"_id": scan_id}, raw, upsert=True)

            # Write to lightweight index
            await self._db[_INDEX_COLLECTION].replace_one(
                {"scan_id": scan_id},
                {
                    "scan_id": scan_id,
                    "account_id": doc.metadata.account_id,
                    "started_at": doc.metadata.started_at,
                    "completed_at": doc.metadata.completed_at,
                    "regions_scanned": doc.metadata.regions_scanned,
                    "node_count": doc.metadata.node_count,
                    "edge_count": doc.metadata.edge_count,
                },
                upsert=True,
            )
            log.info(
                "mongo.scan_saved",
                scan_id=scan_id,
                nodes=doc.metadata.node_count,
                edges=doc.metadata.edge_count,
            )
            return scan_id
        except Exception as exc:
            raise StorageError(f"MongoDB save failed: {exc}") from exc

    async def get_latest_scan(self, account_id: str) -> dict[str, Any] | None:
        assert self._db is not None  # noqa: S101
        cursor = (
            self._db[_SCANS_COLLECTION]
            .find({"metadata.account_id": account_id})
            .sort("metadata.started_at", DESCENDING)
            .limit(1)
        )
        docs = await cursor.to_list(length=1)
        return docs[0] if docs else None

    async def list_scans(self, account_id: str, limit: int = 20) -> list[dict[str, Any]]:
        assert self._db is not None  # noqa: S101
        cursor = (
            self._db[_INDEX_COLLECTION]
            .find({"account_id": account_id})
            .sort("started_at", DESCENDING)
            .limit(limit)
        )
        return await cursor.to_list(length=limit)
