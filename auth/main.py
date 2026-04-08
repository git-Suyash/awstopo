"""
Secure STS credential management.

Design principles:
  - Zero static credentials. The executor's identity (EC2 instance role,
    ECS task role, Lambda execution role, or an OIDC-federated identity)
    is used to assume the target role via STS AssumeRole.
  - ExternalId prevents confused-deputy attacks when this tool is used as
    a third-party service.
  - Credentials are refreshed automatically before they expire.
  - No secrets are ever logged.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TypedDict

import aioboto3
import structlog

from config.main import AWSSettings
from exceptions.main import AuthError


log = structlog.get_logger(__name__)

# Refresh 5 minutes before expiry to avoid mid-flight expiry
_REFRESH_BUFFER = timedelta(minutes=5)


class STSCredentials(TypedDict):
    aws_access_key_id: str
    aws_secret_access_key: str
    aws_session_token: str


class CredentialProvider:
    """
    Manages a single set of temporary STS credentials for a target account.

    Thread-safe and asyncio-safe: uses a lock to prevent thundering-herd
    refreshes when multiple collectors run concurrently.
    """

    def __init__(
        self,
        role_arn: str,
        external_id: str,
        session_duration_seconds: int = 900,
    ) -> None:
        self._role_arn = role_arn
        self._external_id = external_id
        self._session_duration = session_duration_seconds
        self._credentials: STSCredentials | None = None
        self._expiry: datetime | None = None
        self._lock = asyncio.Lock()

    @classmethod
    def from_settings(cls, settings: AWSSettings) -> "CredentialProvider":
        """Convenience constructor for the CLI entry point."""
        return cls(
            role_arn=settings.role_arn,
            external_id=settings.external_id.get_secret_value(),
            session_duration_seconds=settings.session_duration_seconds,
        )

    async def get_credentials(self) -> STSCredentials:
        """Return valid temporary credentials, refreshing if needed."""
        async with self._lock:
            if self._needs_refresh():
                await self._refresh()
        assert self._credentials is not None  # noqa: S101
        return self._credentials

    def _needs_refresh(self) -> bool:
        if self._credentials is None or self._expiry is None:
            return True
        return datetime.now(UTC) >= self._expiry - _REFRESH_BUFFER

    async def _refresh(self) -> None:
        log.info("sts.assume_role.start", role_arn=self._role_arn)
        session = aioboto3.Session()
        try:
            async with session.client("sts") as sts:
                response = await sts.assume_role(
                    RoleArn=self._role_arn,
                    RoleSessionName="AWSMapperScan",
                    ExternalId=self._external_id,
                    DurationSeconds=self._session_duration,
                )
        except Exception as exc:
            raise AuthError(f"STS AssumeRole failed: {exc}") from exc

        creds = response["Credentials"]
        self._credentials = STSCredentials(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
        )
        self._expiry = creds["Expiration"]
        log.info(
            "sts.assume_role.success",
            role_arn=self._role_arn,
            expires_at=self._expiry.isoformat(),
        )

    async def get_session(self, region: str) -> aioboto3.Session:
        """Return an aioboto3 Session pre-loaded with temporary credentials."""
        creds = await self.get_credentials()
        return aioboto3.Session(
            aws_access_key_id=creds["aws_access_key_id"],
            aws_secret_access_key=creds["aws_secret_access_key"],
            aws_session_token=creds["aws_session_token"],
            region_name=region,
        )


async def discover_enabled_regions(credential_provider: CredentialProvider) -> list[str]:
    """
    Return all regions that are enabled for this account.
    Falls back to a safe default set if the API call fails.
    """
    session = await credential_provider.get_session("us-east-1")
    try:
        async with session.client("ec2") as ec2:
            resp = await ec2.describe_regions(
                Filters=[{"Name": "opt-in-status", "Values": ["opt-in-not-required", "opted-in"]}]
            )
            regions = [r["RegionName"] for r in resp["Regions"]]
            log.info("regions.discovered", count=len(regions))
            return regions
    except Exception as exc:
        log.warning("regions.discover_failed", reason=str(exc))
        # Safe fallback — major regions
        return [
            "us-east-1", "us-east-2", "us-west-1", "us-west-2",
            "eu-west-1", "eu-central-1", "ap-southeast-1", "ap-northeast-1",
        ]
