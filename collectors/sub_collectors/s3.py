"""
S3 collector.

S3 is a global service — buckets are listed once, then per-bucket details
(region, encryption, versioning, public access block, etc.) are fetched
concurrently with a semaphore to avoid hammering the API.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import aioboto3
import structlog
from botocore.exceptions import ClientError

from collectors.base_collector import BaseCollector

from collectors.resources import (
    BaseResource,
    S3BlockPublicAccess,
    S3BucketResource,
    parse_tags,
)
from config.main import PipelineSettings

log = structlog.get_logger(__name__)

# Limit concurrent per-bucket detail calls
_BUCKET_CONCURRENCY = 10


class S3Collector(BaseCollector):
    """
    S3 collector.

    Note: `region` here is the region of the STS session used to list buckets.
    Each bucket's actual region is discovered per-bucket.
    """

    service = "s3"

    def __init__(
        self,
        session: aioboto3.Session,
        region: str,
        account_id: str,
        settings: PipelineSettings,
    ) -> None:
        super().__init__(session, region, account_id, settings)
        self._sem = asyncio.Semaphore(_BUCKET_CONCURRENCY)

    async def collect(self) -> list[BaseResource]:
        # List all buckets (global call — use us-east-1 to avoid redirect issues)
        raw_buckets = await self._call("list_buckets", "Buckets")
        if not raw_buckets:
            return []

        self._log.info("s3.buckets_found", count=len(raw_buckets))
        tasks = [self._enrich_bucket(b) for b in raw_buckets]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        resources: list[BaseResource] = []
        for r in results:
            if isinstance(r, Exception):
                self._log.warning("s3.bucket_enrich_error", reason=str(r))
            elif r is not None:
                resources.append(r)

        self._log.info("s3.collected", count=len(resources))
        return resources

    async def _enrich_bucket(self, raw: dict) -> S3BucketResource | None:
        async with self._sem:
            name = raw["Name"]
            creation_date: datetime | None = raw.get("CreationDate")

            # Determine bucket region
            bucket_region = await self._get_bucket_region(name)

            tags = await self._get_bucket_tags(name)
            versioning = await self._get_versioning(name)
            encryption = await self._get_encryption(name)
            public_access = await self._get_public_access_block(name)
            has_policy = await self._has_bucket_policy(name)
            logging_enabled = await self._get_logging(name)

            # A bucket is "public" only if public access block is NOT fully active
            is_public = not public_access.fully_blocked

            enc_algo: str | None = None
            sse_enabled = False
            if encryption:
                rules = encryption.get("Rules", [])
                if rules:
                    sse_enabled = True
                    enc_algo = (
                        rules[0]
                        .get("ApplyServerSideEncryptionByDefault", {})
                        .get("SSEAlgorithm")
                    )

            return S3BucketResource(
                resource_id=name,
                region=bucket_region or self._region,
                account_id=self._account_id,
                name=name,
                tags=tags,
                bucket_region=bucket_region or "",
                creation_date=creation_date,
                versioning_enabled=versioning,
                server_side_encryption=sse_enabled,
                encryption_algorithm=enc_algo,
                public_access_block=public_access,
                has_bucket_policy=has_policy,
                is_public=is_public,
                logging_enabled=logging_enabled,
            )

    async def _get_bucket_region(self, bucket: str) -> str | None:
        try:
            async with self._session.client("s3") as s3:
                resp = await s3.get_bucket_location(Bucket=bucket)
                # AWS returns None for us-east-1
                return resp.get("LocationConstraint") or "us-east-1"
        except ClientError:
            return None

    async def _get_bucket_tags(self, bucket: str) -> dict[str, str]:
        try:
            async with self._session.client("s3") as s3:
                resp = await s3.get_bucket_tagging(Bucket=bucket)
                return parse_tags(resp.get("TagSet"))
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchTagSet":
                return {}
            return {}

    async def _get_versioning(self, bucket: str) -> bool:
        try:
            async with self._session.client("s3") as s3:
                resp = await s3.get_bucket_versioning(Bucket=bucket)
                return resp.get("Status") == "Enabled"
        except ClientError:
            return False

    async def _get_encryption(self, bucket: str) -> dict | None:
        try:
            async with self._session.client("s3") as s3:
                resp = await s3.get_bucket_encryption(Bucket=bucket)
                return resp.get("ServerSideEncryptionConfiguration")
        except ClientError as e:
            if e.response["Error"]["Code"] in (
                "ServerSideEncryptionConfigurationNotFoundError",
                "NoSuchBucket",
            ):
                return None
            return None

    async def _get_public_access_block(self, bucket: str) -> S3BlockPublicAccess:
        try:
            async with self._session.client("s3") as s3:
                resp = await s3.get_public_access_block(Bucket=bucket)
                cfg = resp.get("PublicAccessBlockConfiguration", {})
                return S3BlockPublicAccess(
                    block_public_acls=cfg.get("BlockPublicAcls", False),
                    ignore_public_acls=cfg.get("IgnorePublicAcls", False),
                    block_public_policy=cfg.get("BlockPublicPolicy", False),
                    restrict_public_buckets=cfg.get("RestrictPublicBuckets", False),
                )
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchPublicAccessBlockConfiguration":
                # No block configured → potentially public
                return S3BlockPublicAccess(
                    block_public_acls=False,
                    ignore_public_acls=False,
                    block_public_policy=False,
                    restrict_public_buckets=False,
                )
            return S3BlockPublicAccess()  # default to safe

    async def _has_bucket_policy(self, bucket: str) -> bool:
        try:
            async with self._session.client("s3") as s3:
                await s3.get_bucket_policy(Bucket=bucket)
                return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchBucketPolicy":
                return False
            return False

    async def _get_logging(self, bucket: str) -> bool:
        try:
            async with self._session.client("s3") as s3:
                resp = await s3.get_bucket_logging(Bucket=bucket)
                return "LoggingEnabled" in resp
        except ClientError:
            return False
