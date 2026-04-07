"""
Abstract base class for all AWS resource collectors.

Provides:
  - Async context manager for the boto3 client
  - Automatic pagination via aioboto3 paginators
  - Retry integration
  - Structured logging
  - Uniform error handling (logs and records error, does not crash pipeline)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import aioboto3
import structlog
from botocore.exceptions import ClientError

from collectors.resources import BaseResource
from config.main import PipelineSettings
from core.retry import with_retry
from exceptions.main import CollectorError, ThrottleError


log = structlog.get_logger(__name__)

_THROTTLE_CODES = frozenset(
    {"Throttling", "ThrottlingException", "RequestLimitExceeded", "TooManyRequestsException"}
)


class BaseCollector(ABC):
    """
    Subclass this for each AWS service / resource type.

    Example:
        class VPCCollector(BaseCollector):
            service = "ec2"

            async def collect(self) -> list[BaseResource]:
                ...
    """

    service: str  # AWS service name, e.g. "ec2", "rds", "s3"

    def __init__(
        self,
        session: aioboto3.Session,
        region: str,
        account_id: str,
        settings: PipelineSettings,
    ) -> None:
        self._session = session
        self._region = region
        self._account_id = account_id
        self._settings = settings
        self._log = log.bind(collector=self.__class__.__name__, region=region)

    @abstractmethod
    async def collect(self) -> list[BaseResource]:
        """Execute all API calls and return a list of typed resource models."""
        ...

    async def safe_collect(self) -> tuple[list[BaseResource], str | None]:
        """
        Wrapper that catches all errors and returns (resources, error_message).

        Use this in the pipeline so one collector failure does not abort others.
        """
        try:
            resources = await self.collect()
            self._log.info("collector.done", count=len(resources))
            return resources, None
        except CollectorError as exc:
            self._log.error("collector.error", reason=str(exc))
            return [], str(exc)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "Unknown")
            msg = f"ClientError [{code}]: {exc}"
            self._log.error("collector.client_error", code=code, reason=msg)
            return [], msg
        except Exception as exc:
            self._log.exception("collector.unexpected_error", reason=str(exc))
            return [], str(exc)

    async def _paginate(self, method_name: str, result_key: str, **kwargs: Any) -> list[Any]:
        """
        Paginate an EC2/RDS-style API call and return all items.

        Args:
            method_name: e.g. "describe_instances"
            result_key: top-level key in the response, e.g. "Reservations"
            **kwargs: passed directly to the paginator
        """
        items: list[Any] = []

        async def _call() -> list[Any]:
            _items: list[Any] = []
            async with self._session.client(self.service) as client:
                try:
                    paginator = client.get_paginator(method_name)
                    async for page in paginator.paginate(**kwargs):
                        _items.extend(page.get(result_key, []))
                except client.exceptions.InvalidAction:  # type: ignore[attr-defined]
                    # Paginator not supported — fall back to direct call
                    response = await getattr(client, method_name)(**kwargs)
                    _items.extend(response.get(result_key, []))
            return _items

        try:
            items = await with_retry(self._settings, _call)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in _THROTTLE_CODES:
                raise ThrottleError(self.service, self._region, str(exc)) from exc
            raise CollectorError(self.service, self._region, str(exc)) from exc

        return items

    async def _call(self, method_name: str, result_key: str, **kwargs: Any) -> Any:
        """
        Single (non-paginated) API call with retry.
        """

        async def _inner() -> Any:
            async with self._session.client(self.service) as client:
                response = await getattr(client, method_name)(**kwargs)
                return response.get(result_key)

        try:
            return await with_retry(self._settings, _inner)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in _THROTTLE_CODES:
                raise ThrottleError(self.service, self._region, str(exc)) from exc
            raise CollectorError(self.service, self._region, str(exc)) from exc
