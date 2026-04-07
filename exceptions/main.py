"""Domain-specific exceptions for aws-mapper."""

from __future__ import annotations


class AWSMapperError(Exception):
    """Base exception for all aws-mapper errors."""


class AuthError(AWSMapperError):
    """Raised when STS assume-role or credential refresh fails."""


class CollectorError(AWSMapperError):
    """Raised when an AWS API collector encounters an unrecoverable error."""

    def __init__(self, service: str, region: str, reason: str) -> None:
        self.service = service
        self.region = region
        super().__init__(f"[{service}/{region}] {reason}")


class ProcessingError(AWSMapperError):
    """Raised when resource data fails validation or processing."""


class StorageError(AWSMapperError):
    """Raised when writing to MongoDB or Neo4j fails."""


class PipelineError(AWSMapperError):
    """Raised when the overall pipeline fails to complete."""


class ThrottleError(CollectorError):
    """AWS API throttling — signals the retry layer to back off."""
