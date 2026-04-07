"""
Async pipeline engine.

Pipeline stages:
  COLLECT → PROCESS → STORE

Each stage is a coroutine. Stages run in sequence per (service, region) pair,
but multiple (service, region) pairs run concurrently up to PIPELINE_CONCURRENCY.

Failures in one (service, region) are isolated — they emit a warning and the
pipeline continues with partial data.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

import structlog

from config.main import PipelineSettings



log = structlog.get_logger(__name__)


class StageStatus(Enum):
    PENDING = auto()
    RUNNING = auto()
    DONE = auto()
    FAILED = auto()
    SKIPPED = auto()


@dataclass
class StageResult:
    name: str
    status: StageStatus
    data: Any = None
    error: Exception | None = None


@dataclass
class PipelineResult:
    account_id: str
    region: str
    stages: list[StageResult] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return all(s.status in (StageStatus.DONE, StageStatus.SKIPPED) for s in self.stages)

    @property
    def partial(self) -> bool:
        return any(s.status == StageStatus.FAILED for s in self.stages)


class Pipeline:
    """
    Runs a sequence of named async stages, collecting results from each.

    Usage:
        pipeline = Pipeline(settings)
        result = await pipeline.run(
            account_id="123",
            region="us-east-1",
            stages=[
                ("collect_vpc",  collect_vpc_fn),
                ("collect_ec2",  collect_ec2_fn),
                ("process",      process_fn),
                ("store",        store_fn),
            ]
        )
    """

    def __init__(self, settings: PipelineSettings) -> None:
        self._settings = settings
        self._semaphore = asyncio.Semaphore(settings.concurrency)

    async def run(
        self,
        account_id: str,
        region: str,
        stages: list[tuple[str, Any]],
    ) -> PipelineResult:
        result = PipelineResult(account_id=account_id, region=region)
        context: dict[str, Any] = {}

        async with self._semaphore:
            for name, fn in stages:
                log.info("stage.start", stage=name, region=region)
                stage_result = await self._run_stage(name, fn, context)
                result.stages.append(stage_result)
                if stage_result.status == StageStatus.DONE:
                    context[name] = stage_result.data
                elif stage_result.status == StageStatus.FAILED:
                    log.error(
                        "stage.failed",
                        stage=name,
                        region=region,
                        error=str(stage_result.error),
                    )
                    # Propagate what we have so far; later stages may handle partial data

        return result

    async def _run_stage(
        self,
        name: str,
        fn: Any,
        context: dict[str, Any],
    ) -> StageResult:
        try:
            data = await fn(context)
            log.info("stage.done", stage=name)
            return StageResult(name=name, status=StageStatus.DONE, data=data)
        except Exception as exc:
            log.exception("stage.error", stage=name, exc=str(exc))
            return StageResult(name=name, status=StageStatus.FAILED, error=exc)


async def run_all_regions(
    regions: list[str],
    pipeline_factory: Any,  # Callable[[str], Pipeline coroutine
    settings: PipelineSettings,
) -> list[PipelineResult]:
    """Run one pipeline per region concurrently, bounded by semaphore inside Pipeline."""
    tasks = [pipeline_factory(region) for region in regions]
    results: list[PipelineResult] = await asyncio.gather(*tasks, return_exceptions=False)
    return results
