"""Single-job worker orchestration with chunk-level bounded concurrency."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import TypeVar

import structlog

from app.adapters.persistence.worker import LeaseLostError, WorkerPersistence
from app.config import Settings
from app.core.errors import DocumentError, ErrorCode
from app.core.models import (
    Block,
    ChunkRecord,
    JobError,
    JobRecord,
    JobStatus,
    TranslationPlan,
    TriageStatus,
)
from app.core.ports import (
    CostCalculator,
    DocumentRepository,
    FileStorage,
    FormatRegistry,
    JobExecutionRepository,
    LLMProvider,
    TranslationCacheRepository,
)
from app.worker.assembly import Assembly
from app.worker.translation_loop import TranslationLoop

_logger = structlog.get_logger(__name__)
_IDLE_WAIT_SECONDS = 1.0
_T = TypeVar("_T")


class ClaimLoop:
    """Claim one job at a time and supervise its chunks and assembly lease."""

    def __init__(
        self,
        settings: Settings,
        job_repo: JobExecutionRepository,
        cache_repo: TranslationCacheRepository,
        document_repo: DocumentRepository,
        llm_provider: LLMProvider,
        cost_calculator: CostCalculator,
        format_registry: FormatRegistry,
        file_storage: FileStorage,
        *,
        persistence: WorkerPersistence,
        shutdown_event: asyncio.Event | None = None,
    ) -> None:
        self._settings = settings
        self._job_repo = job_repo
        self._cache_repo = cache_repo
        self._document_repo = document_repo
        self._llm_provider = llm_provider
        self._cost_calculator = cost_calculator
        self._format_registry = format_registry
        self._file_storage = file_storage
        self._persistence = persistence
        self._shutdown_event = shutdown_event or asyncio.Event()
        self._active_chunk_tasks: dict[str, asyncio.Task[None]] = {}

    async def run(self) -> None:
        """Recover expired chunk leases, then claim until graceful shutdown."""
        async with self._persistence.write():
            await self._job_repo.release_expired_chunks(datetime.now(UTC))

        while not self._shutdown_event.is_set():
            lease_expires_at = self._lease_expiry(self._settings.job_lease_seconds)
            async with self._persistence.write():
                job = await self._job_repo.claim_job(
                    self._settings.worker_id,
                    lease_expires_at,
                )

            if job is None:
                await self._wait_for_work()
                continue

            _logger.info("worker_job_claimed", job_id=job.id, worker_id=self._settings.worker_id)
            try:
                await self._run_job(job)
            except LeaseLostError:
                _logger.info("worker_lease_lost", job_id=job.id)

    async def _run_job(self, job: JobRecord) -> None:
        heartbeat_task = asyncio.create_task(self._heartbeat(job), name=f"heartbeat:{job.id}")
        self._active_chunk_tasks = {}
        try:
            async with self._persistence.read():
                await self._persistence.ensure_owned(job.id, self._settings.worker_id)
                all_blocks = await self._document_repo.get_blocks(job.document_id)
                analysis = await self._document_repo.get_analysis(job.document_id)

            plan = (
                TranslationPlan(
                    source_language=analysis.source_language,
                    domain=analysis.domain,
                    register=analysis.register,
                    terms=analysis.terms,
                    warnings=analysis.warnings,
                    triage_status=analysis.triage_status,
                )
                if analysis is not None
                else TranslationPlan(
                    source_language="und",
                    domain="general",
                    register="neutral",
                    warnings=["Document analysis was unavailable; using the degraded plan."],
                    triage_status=TriageStatus.DEGRADED,
                )
            )

            if job.status is JobStatus.ASSEMBLING:
                async with self._persistence.read():
                    unfinished = await self._persistence.unfinished_chunks(job.id)
                if unfinished:
                    # Assembly is entered only after all chunks are terminal. A
                    # partial assembling job is an invariant violation; retain
                    # its lease and let the next restart recover it safely.
                    raise RuntimeError("assembling job has unfinished chunks")
            else:
                await self._process_chunks(job, all_blocks, plan, heartbeat_task)

            if job.status is not JobStatus.ASSEMBLING:
                await self._race_heartbeat(self._mark_assembling(job), heartbeat_task)
            assembly = Assembly(
                self._job_repo,
                self._cache_repo,
                self._document_repo,
                self._format_registry,
                self._file_storage,
                persistence=self._persistence,
            )
            try:
                await self._race_heartbeat(assembly.render(job, plan), heartbeat_task)
            except LeaseLostError:
                raise
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._race_heartbeat(self._fail_render(job), heartbeat_task)
                _logger.error(
                    "worker_render_failed",
                    job_id=job.id,
                    document_id=job.document_id,
                    error_code=ErrorCode.RENDER_FAILED.value,
                )
        finally:
            await self._cancel_active_chunks()
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)

    async def _process_chunks(
        self,
        job: JobRecord,
        all_blocks: list[Block],
        plan: TranslationPlan,
        heartbeat_task: asyncio.Task[None],
    ) -> None:
        translation_loop = TranslationLoop(
            self._settings,
            self._job_repo,
            self._cache_repo,
            self._llm_provider,
            self._cost_calculator,
            persistence=self._persistence,
        )
        limit = self._settings.max_chunk_concurrency

        while True:
            while len(self._active_chunk_tasks) < limit:
                chunk = await self._race_heartbeat(self._claim_chunk(job), heartbeat_task)
                if chunk is None:
                    break
                async with self._persistence.read():
                    await self._persistence.ensure_owned(job.id, self._settings.worker_id, chunk.id)
                    blocks = await self._persistence.get_chunk_blocks(chunk.id)
                task = asyncio.create_task(
                    translation_loop.process_chunk(job, chunk, blocks, all_blocks, plan),
                    name=f"translate:{chunk.id}",
                )
                self._active_chunk_tasks[chunk.id] = task

            if self._active_chunk_tasks:
                completed, _ = await asyncio.wait(
                    {*self._active_chunk_tasks.values(), heartbeat_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if heartbeat_task in completed:
                    heartbeat_task.result()
                for chunk_id, task in tuple(self._active_chunk_tasks.items()):
                    if task in completed:
                        try:
                            await task
                        finally:
                            self._active_chunk_tasks.pop(chunk_id, None)
                continue

            async with self._persistence.read():
                unfinished = await self._persistence.unfinished_chunks(job.id)
            if not unfinished:
                return

            # A recovered job can still have chunks leased by another live
            # worker even after its job lease expired. Poll until those leases
            # expire instead of assembling before their work is checkpointed.
            await self._race_heartbeat(
                asyncio.sleep(self._settings.heartbeat_interval_seconds),
                heartbeat_task,
            )

    async def _claim_chunk(self, job: JobRecord) -> ChunkRecord | None:
        async with self._persistence.write():
            await self._job_repo.release_expired_chunks(datetime.now(UTC))
            await self._persistence.ensure_owned(job.id, self._settings.worker_id)
            return await self._job_repo.claim_chunk(
                job.id,
                self._settings.worker_id,
                self._lease_expiry(self._settings.chunk_lease_seconds),
            )

    async def _mark_assembling(self, job: JobRecord) -> None:
        async with self._persistence.write():
            await self._persistence.ensure_owned(job.id, self._settings.worker_id)
            await self._job_repo.complete_job(job.id, JobStatus.ASSEMBLING)

    async def _fail_render(self, job: JobRecord) -> None:
        error = JobError(
            error_code=ErrorCode.RENDER_FAILED.value,
            message=DocumentError(ErrorCode.RENDER_FAILED).message,
            retryable=False,
        )
        async with self._persistence.write():
            await self._persistence.ensure_owned(job.id, self._settings.worker_id)
            await self._job_repo.complete_job(job.id, JobStatus.FAILED, error)

    async def _heartbeat(self, job: JobRecord) -> None:
        interval = self._settings.heartbeat_interval_seconds
        while True:
            await asyncio.sleep(interval)
            async with self._persistence.write():
                now = datetime.now(UTC)
                await self._persistence.ensure_owned(job.id, self._settings.worker_id)
                await self._job_repo.heartbeat_job(
                    job.id,
                    now + timedelta(seconds=self._settings.job_lease_seconds),
                )
                _logger.info("worker_heartbeat", job_id=job.id)
                for chunk_id, task in tuple(self._active_chunk_tasks.items()):
                    # A completed task has already durably checkpointed its
                    # terminal chunk state. It no longer needs a chunk lease.
                    if task.done():
                        continue
                    try:
                        await self._persistence.ensure_owned(
                            job.id,
                            self._settings.worker_id,
                            chunk_id,
                        )
                    except LeaseLostError:
                        if task.done():
                            continue
                        raise
                    await self._job_repo.heartbeat_chunk(
                        chunk_id,
                        now + timedelta(seconds=self._settings.chunk_lease_seconds),
                    )

    async def _wait_for_work(self) -> None:
        with suppress(TimeoutError):
            await asyncio.wait_for(self._shutdown_event.wait(), timeout=_IDLE_WAIT_SECONDS)

    async def _race_heartbeat(
        self,
        awaitable: Awaitable[_T],
        heartbeat_task: asyncio.Task[None],
    ) -> _T:
        operation = asyncio.ensure_future(awaitable)
        try:
            done, _ = await asyncio.wait(
                {operation, heartbeat_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if operation in done:
                return operation.result()
            heartbeat_task.result()
            return operation.result()
        finally:
            if not operation.done():
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)

    async def _cancel_active_chunks(self) -> None:
        tasks = list(self._active_chunk_tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._active_chunk_tasks.clear()

    def _lease_expiry(self, seconds: int) -> datetime:
        return datetime.now(UTC) + timedelta(seconds=seconds)
