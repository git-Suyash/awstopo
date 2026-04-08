"""Centralised, validated configuration loaded from environment / .env file."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AWSSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AWS_", env_file=".env", extra="ignore")

    role_arn: str = Field(..., description="ARN of the IAM role to assume in the target account")
    external_id: SecretStr = Field(..., description="External ID for confused-deputy protection")
    regions: list[str] = Field(default_factory=list, description="Regions to scan; empty = all enabled")
    session_duration_seconds: Annotated[int, Field(ge=900, le=3600)] = 900

    @field_validator("role_arn")
    @classmethod
    def validate_role_arn(cls, v: str) -> str:
        if not v.startswith("arn:aws"):
            raise ValueError("role_arn must be a valid AWS ARN starting with 'arn:aws'")
        return v

    @field_validator("regions", mode="before")
    @classmethod
    def parse_regions(cls, v: object) -> list[str]:
        if isinstance(v, str):
            return [r.strip() for r in v.split(",") if r.strip()]
        return list(v)  # type: ignore[arg-type]


class MongoSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MONGO_", env_file=".env", extra="ignore")

    uri: SecretStr = Field(default=SecretStr("mongodb://localhost:27017"))
    db: str = "aws_mapper"
    tls: bool = False
    tls_ca_file: str | None = None


class Neo4jSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NEO4J_", env_file=".env", extra="ignore")

    uri: str = "bolt://localhost:7687"
    user: str = "neo4j"
    password: SecretStr = Field(default=SecretStr("neo4j"))
    database: str = "neo4j"


class PipelineSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PIPELINE_", env_file=".env", extra="ignore")

    max_retries: Annotated[int, Field(ge=1, le=10)] = 3
    retry_wait_min_seconds: float = 2.0
    retry_wait_max_seconds: float = 30.0
    concurrency: Annotated[int, Field(ge=1, le=20)] = 5


class LogSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LOG_", env_file=".env", extra="ignore")

    level: str = "INFO"
    format: str = "json"  # "json" | "console"


class AuthSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AUTH_", env_file=".env", extra="ignore")

    jwt_secret: SecretStr = Field(..., description="Secret key for signing JWTs")
    jwt_algorithm: str = "HS256"
    jwt_expiry_minutes: int = Field(default=60, ge=5)


class Settings(BaseSettings):
    """Aggregate settings object — single source of truth."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    aws: AWSSettings = Field(default_factory=AWSSettings)  # type: ignore[call-arg]
    mongo: MongoSettings = Field(default_factory=MongoSettings)  # type: ignore[call-arg]
    neo4j: Neo4jSettings = Field(default_factory=Neo4jSettings)  # type: ignore[call-arg]
    pipeline: PipelineSettings = Field(default_factory=PipelineSettings)  # type: ignore[call-arg]
    log: LogSettings = Field(default_factory=LogSettings)  # type: ignore[call-arg]
    auth: AuthSettings = Field(default_factory=AuthSettings)  # type: ignore[call-arg]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings singleton."""
    return Settings()
