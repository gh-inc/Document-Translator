"""Crash and resume test: committed blocks never return to the provider."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.adapters.formats.docx import DocxExtractor, DocxRenderer
from app.adapters.formats.pdf import PdfExtractor, PdfRenderer
from app.adapters.formats.registry import FormatRegistry
from app.adapters.llm.fake_provider import FakeProvider
from app.adapters.llm.pricing import ModelCostCalculator
from app.adapters.persistence.database import SqliteConnectionFactory, transaction
from app.adapters.persistence.repositories import (
    SqliteDocumentRepository,
    SqliteJobExecutionRepository,
    SqliteTranslationCacheRepository,
)
from app.adapters.persistence.worker import WorkerPersistence
from app.adapters.storage.filesystem import FilesystemStorage
from app.config import Settings
from app.core.models import (
    ChunkBlockRecord,
    ChunkRecord,
    ChunkStatus,
    JobRecord,
    JobStatus,
)
from app.worker.claim_loop import ClaimLoop
from app.worker.keys import translation_key


class StopAfterCheckpointProvider(FakeProvider):
    """Complete one call, then hold the next provider request until cancellation."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings=settings, fail_rate=0, latency_ms=0)
        self.first_call_returned = asyncio.Event()
        self.second_call_started = asyncio.Event()
        self.requested_block_ids: list[str] = []

    async def translate_chunk(self, request):
        self.requested_block_ids.extend(block.id for block in request.blocks)
        if len(self.requested_block_ids) == 1:
            result = await super().translate_chunk(request)
            self.first_call_returned.set()
            return result
        self.second_call_started.set()
        await asyncio.Event().wait()
        raise AssertionError("the held provider call should be cancelled")


class TrackingFakeProvider(FakeProvider):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings=settings, fail_rate=0, latency_ms=0)
        self.requested_block_ids: list[str] = []

    async def translate_chunk(self, request):
        self.requested_block_ids.extend(block.id for block in request.blocks)
        return await super().translate_chunk(request)


async def _wait_for_job(
    persistence: WorkerPersistence,
    job_repo: SqliteJobExecutionRepository,
    job_id: str,
    statuses: set[JobStatus],
) -> JobRecord:
    async with asyncio.timeout(20):
        while True:
            async with persistence.read():
                job = await job_repo.get_job(job_id)
            if job is not None and job.status in statuses:
                return job
            await asyncio.sleep(0.01)


async def test_restart_skips_every_translation_committed_before_cancellation(
    tmp_path: Path,
) -> None:
    first_settings = Settings(
        database_path=tmp_path / "resume.db",
        upload_storage_path=tmp_path / "uploads",
        output_storage_path=tmp_path / "outputs",
        worker_id="worker-before-crash",
        heartbeat_interval_seconds=1,
        job_lease_seconds=5,
        chunk_lease_seconds=5,
        max_chunk_concurrency=1,
        max_chunk_attempts=2,
    )
    connection = await SqliteConnectionFactory(first_settings.database_path).create()
    first_shutdown = asyncio.Event()
    second_shutdown = asyncio.Event()
    first_run: asyncio.Task[None] | None = None
    second_run: asyncio.Task[None] | None = None
    try:
        storage = FilesystemStorage(
            first_settings.upload_storage_path,
            first_settings.output_storage_path,
        )
        document_repo = SqliteDocumentRepository(connection)
        cache_repo = SqliteTranslationCacheRepository(connection)
        first_persistence = WorkerPersistence(connection)
        first_job_repo = SqliteJobExecutionRepository(
            connection,
            worker_id=first_settings.worker_id,
        )
        formats = FormatRegistry()
        formats.register("pdf", PdfExtractor(), PdfRenderer())
        formats.register("docx", DocxExtractor(), DocxRenderer())

        document_id = "resume-document"
        sample_path = Path(__file__).parents[2] / "samples" / "sample_en.docx"
        content = await asyncio.to_thread(sample_path.read_bytes)
        upload_path = await storage.save_upload(document_id, content, sample_path.name)
        resolved = await formats.resolve(upload_path)
        assert resolved is not None
        document_ir = await resolved[0].extract(upload_path, document_id)
        assert len(document_ir.blocks) >= 2
        async with transaction(connection):
            await document_repo.create_document(
                document_id,
                document_ir.filename,
                "docx",
                document_ir.size_bytes,
                str(upload_path),
                document_ir.page_count,
            )
            await document_repo.create_blocks(document_id, document_ir.blocks)

        now = datetime.now(UTC)
        job = JobRecord(
            id="resume-job",
            document_id=document_id,
            batch_id="resume-batch",
            target_language="de",
            status=JobStatus.QUEUED,
            total_chunks=len(document_ir.blocks),
            done_chunks=0,
            model="gpt-4o-mini",
            prompt_version="resume-test-v1",
            tokens_in=0,
            tokens_out=0,
            cost_usd=0,
            error_code=None,
            error_detail=None,
            idempotency_key="resume-request",
            lease_owner=None,
            lease_expires_at=None,
            created_at=now,
            updated_at=now,
        )
        chunks = [
            ChunkRecord(
                id=f"resume-chunk-{index}",
                job_id=job.id,
                seq=index,
                status=ChunkStatus.PENDING,
                lease_owner=None,
                lease_expires_at=None,
                created_at=now,
            )
            for index, _ in enumerate(document_ir.blocks)
        ]
        links = [
            ChunkBlockRecord(chunk_id=chunk.id, block_id=block.id, seq_in_chunk=0)
            for chunk, block in zip(chunks, document_ir.blocks, strict=True)
        ]
        await SqliteJobExecutionRepository(connection).create_job_with_chunks(job, chunks, links)

        first_provider = StopAfterCheckpointProvider(first_settings)
        first_worker = ClaimLoop(
            first_settings,
            first_job_repo,
            cache_repo,
            document_repo,
            first_provider,
            ModelCostCalculator(),
            formats,
            storage,
            persistence=first_persistence,
            shutdown_event=first_shutdown,
        )
        first_run = asyncio.create_task(first_worker.run())
        await asyncio.wait_for(first_provider.second_call_started.wait(), timeout=10)
        committed_block_id = first_provider.requested_block_ids[0]
        async with first_persistence.read():
            committed_translation = await cache_repo.get_block_translation(
                translation_key(job),
                committed_block_id,
            )
            claimed_job = await first_job_repo.get_job(job.id)
        assert committed_translation is not None
        assert claimed_job is not None
        assert claimed_job.done_chunks == 1

        first_run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_run
        first_run = None

        # Simulate process downtime longer than both lease windows.
        async with transaction(connection):
            async with connection.execute(
                "UPDATE jobs SET lease_expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
                (job.id,),
            ):
                pass
            async with connection.execute(
                "UPDATE chunks SET lease_expires_at = '2000-01-01T00:00:00+00:00' "
                "WHERE job_id = ? AND status = 'inflight'",
                (job.id,),
            ):
                pass

        await connection.close()
        connection = await SqliteConnectionFactory(
            first_settings.database_path,
            init_schema=False,
        ).create()
        document_repo = SqliteDocumentRepository(connection)
        cache_repo = SqliteTranslationCacheRepository(connection)
        second_persistence = WorkerPersistence(connection)
        second_job_repo = SqliteJobExecutionRepository(
            connection,
            worker_id="worker-after-restart",
        )

        second_settings = first_settings.model_copy(update={"worker_id": "worker-after-restart"})
        second_provider = TrackingFakeProvider(second_settings)
        second_worker = ClaimLoop(
            second_settings,
            second_job_repo,
            cache_repo,
            document_repo,
            second_provider,
            ModelCostCalculator(),
            formats,
            storage,
            persistence=second_persistence,
            shutdown_event=second_shutdown,
        )
        second_run = asyncio.create_task(second_worker.run())
        result = await _wait_for_job(
            second_persistence,
            second_job_repo,
            job.id,
            {JobStatus.DONE, JobStatus.COMPLETED_WITH_ERRORS, JobStatus.FAILED},
        )
        second_shutdown.set()
        await second_run
        second_run = None

        assert result.status is JobStatus.DONE
        assert committed_block_id not in second_provider.requested_block_ids
    finally:
        for task, shutdown in (
            (first_run, first_shutdown),
            (second_run, second_shutdown),
        ):
            if task is not None and not task.done():
                shutdown.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        await connection.close()
