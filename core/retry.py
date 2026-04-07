"""
Retry utilities for AWS API calls.

Separates two concerns:
  1. Transient errors (throttling, 5xx) → exponential back-off + retry
  2. Permanent errors (access denied, resource not found) → fail fast
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

import structlog
from botocore.exceptions import ClientError
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
    wait_random,
)

from config.main import PipelineSettings
from exceptions.main import ThrottleError

log = structlog.get_logger(__name__)

F = TypeVar("F")

# AWS error codes that are transient and safe to retry
_RETRYABLE_CODES = frozenset(
    {
        "Throttling",
        "ThrottlingException",
        "RequestLimitExceeded",
        "RequestThrottled",
        "TooManyRequestsException",
        "ServiceUnavailable",
        "InternalError",
        "InternalFailure",
        "RequestExpired",
    }
)


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, ThrottleError):
        return True
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        return code in _RETRYABLE_CODES
    return False


def _log_retry_attempt(retry_state: RetryCallState) -> None:
    log.warning(
        "retry.attempt",
        attempt=retry_state.attempt_number,
        wait=retry_state.next_action.sleep if retry_state.next_action else None,
        exc=str(retry_state.outcome.exception()) if retry_state.outcome else None,
    )


def make_retrying(settings: PipelineSettings) -> AsyncRetrying:
    """Return a configured AsyncRetrying instance."""
    return AsyncRetrying(
        stop=stop_after_attempt(settings.max_retries),
        wait=(
            wait_exponential(
                min=settings.retry_wait_min_seconds,
                max=settings.retry_wait_max_seconds,
            )
            + wait_random(0, 1)  # jitter to avoid thundering herd
        ),
        retry=retry_if_exception(_is_retryable),
        before_sleep=_log_retry_attempt,
        reraise=True,
    )


async def with_retry(
    settings: PipelineSettings,
    fn: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Execute *fn* with retry semantics. Propagates non-retryable errors immediately."""
    async for attempt in make_retrying(settings):
        with attempt:
            return await fn(*args, **kwargs)
