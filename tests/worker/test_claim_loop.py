"""Lease supervision tests for provider execution and slow assembly."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

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
from app.core.errors import DocumentError, ErrorCode
from app.core.models import (
    ChunkBlockRecord,
    ChunkRecord,
    ChunkStatus,
    JobRecord,
    JobStatus,
)
from app.worker import claim_loop
from app.worker.claim_loop import ClaimLoop
from app.worker.keys import translation_key


class GatedFakeProvider(FakeProvider):
    def __init__(self, settings: Settings, connection) -> None:
        super().__init__(settings=settings, fail_rate=0, latency_ms=0)
        self.connection = connection
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def translate_chunk(self, request):
        assert not self.connection.in_transaction
        self.started.set()
        await self.release.wait()
        assert not self.connection.in_transaction
        return await super().translate_chunk(request)


class TrackingFakeProvider(FakeProvider):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings=settings, fail_rate=0, latency_ms=0)
        self.calls = 0

    async def translate_chunk(self, request):
        self.calls += 1
        return await super().translate_chunk(request)


class GatedDocxRenderer(DocxRenderer):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def render(self, original_path, blocks, translations, output_path):
        self.started.set()
        await self.release.wait()
        return await super().render(original_path, blocks, translations, output_path)


async def _assert_lease_has_time_left(
    persistence: WorkerPersistence,
    job_repo: SqliteJobExecutionRepository,
    job_id: str,
) -> None:
    async with persistence.read():
        job = await job_repo.get_job(job_id)
    assert job is not None
    assert job.lease_expires_at is not None
    assert job.lease_expires_at > datetime.now(UTC) + timedelta(seconds=1.5)


async def test_claim_loop_heartbeats_during_provider_and_slow_render(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "heartbeat.db",
        upload_storage_path=tmp_path / "uploads",
        output_storage_path=tmp_path / "outputs",
        worker_id="heartbeat-worker",
        heartbeat_interval_seconds=1,
        job_lease_seconds=3,
        chunk_lease_seconds=3,
        max_chunk_concurrency=1,
    )
    connection = await SqliteConnectionFactory(settings.database_path).create()
    shutdown = asyncio.Event()
    worker_task: asyncio.Task[None] | None = None
    provider: GatedFakeProvider | None = None
    renderer: GatedDocxRenderer | None = None
    try:
        storage = FilesystemStorage(settings.upload_storage_path, settings.output_storage_path)
        document_repo = SqliteDocumentRepository(connection)
        cache_repo = SqliteTranslationCacheRepository(connection)
        persistence = WorkerPersistence(connection)
        job_repo = SqliteJobExecutionRepository(connection, worker_id=settings.worker_id)
        formats = FormatRegistry()
        renderer = GatedDocxRenderer()
        formats.register("docx", DocxExtractor(), renderer)
        formats.register("pdf", PdfExtractor(), PdfRenderer())

        document_id = "heartbeat-document"
        sample_path = Path(__file__).parents[2] / "samples" / "sample_en.docx"
        content = await asyncio.to_thread(sample_path.read_bytes)
        upload_path = await storage.save_upload(document_id, content, sample_path.name)
        resolved = await formats.resolve(upload_path)
        assert resolved is not None
        document_ir = await resolved[0].extract(upload_path, document_id)
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
            id="heartbeat-job",
            document_id=document_id,
            batch_id="heartbeat-batch",
            target_language="de",
            status=JobStatus.QUEUED,
            total_chunks=1,
            done_chunks=0,
            model="gpt-4o-mini",
            prompt_version="heartbeat-v1",
            tokens_in=0,
            tokens_out=0,
            cost_usd=0,
            error_code=None,
            error_detail=None,
            idempotency_key="heartbeat-request",
            lease_owner=None,
            lease_expires_at=None,
            created_at=now,
            updated_at=now,
        )
        chunk = ChunkRecord(
            id="heartbeat-chunk",
            job_id=job.id,
            seq=0,
            status=ChunkStatus.PENDING,
            lease_owner=None,
            lease_expires_at=None,
            created_at=now,
        )
        links = [
            ChunkBlockRecord(chunk_id=chunk.id, block_id=block.id, seq_in_chunk=index)
            for index, block in enumerate(document_ir.blocks)
        ]
        await SqliteJobExecutionRepository(connection).create_job_with_chunks(
            job,
            [chunk],
            links,
        )

        provider = GatedFakeProvider(settings, connection)
        worker = ClaimLoop(
            settings,
            job_repo,
            cache_repo,
            document_repo,
            provider,
            ModelCostCalculator(),
            formats,
            storage,
            persistence=persistence,
            shutdown_event=shutdown,
        )
        worker_task = asyncio.create_task(worker.run())

        await asyncio.wait_for(provider.started.wait(), timeout=5)
        await asyncio.sleep(1.2)
        await _assert_lease_has_time_left(persistence, job_repo, job.id)
        provider.release.set()

        await asyncio.wait_for(renderer.started.wait(), timeout=10)
        await asyncio.sleep(1.2)
        await _assert_lease_has_time_left(persistence, job_repo, job.id)
        renderer.release.set()

        async with asyncio.timeout(10):
            while True:
                async with persistence.read():
                    completed = await job_repo.get_job(job.id)
                if completed is not None and completed.status is JobStatus.DONE:
                    break
                await asyncio.sleep(0.01)
        shutdown.set()
        await worker_task
        worker_task = None
    finally:
        if provider is not None:
            provider.release.set()
        if renderer is not None:
            renderer.release.set()
        if worker_task is not None and not worker_task.done():
            shutdown.set()
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)
        await connection.close()


async def test_claim_loop_waits_for_live_chunk_lease_then_releases_it(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "lease-wait.db",
        upload_storage_path=tmp_path / "uploads",
        output_storage_path=tmp_path / "outputs",
        worker_id="replacement-worker",
        heartbeat_interval_seconds=1,
        job_lease_seconds=5,
        chunk_lease_seconds=3,
        max_chunk_concurrency=1,
    )
    connection = await SqliteConnectionFactory(settings.database_path).create()
    shutdown = asyncio.Event()
    worker_task: asyncio.Task[None] | None = None
    try:
        storage = FilesystemStorage(settings.upload_storage_path, settings.output_storage_path)
        document_repo = SqliteDocumentRepository(connection)
        cache_repo = SqliteTranslationCacheRepository(connection)
        persistence = WorkerPersistence(connection)
        job_repo = SqliteJobExecutionRepository(connection, worker_id=settings.worker_id)
        formats = FormatRegistry()
        formats.register("docx", DocxExtractor(), DocxRenderer())
        formats.register("pdf", PdfExtractor(), PdfRenderer())

        document_id = "leased-document"
        sample_path = Path(__file__).parents[2] / "samples" / "sample_en.docx"
        content = await asyncio.to_thread(sample_path.read_bytes)
        upload_path = await storage.save_upload(document_id, content, sample_path.name)
        resolved = await formats.resolve(upload_path)
        assert resolved is not None
        document_ir = await resolved[0].extract(upload_path, document_id)
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
            id="leased-job",
            document_id=document_id,
            batch_id="leased-batch",
            target_language="de",
            status=JobStatus.RUNNING,
            total_chunks=1,
            done_chunks=0,
            model="gpt-4o-mini",
            prompt_version="lease-wait-v1",
            tokens_in=0,
            tokens_out=0,
            cost_usd=0,
            error_code=None,
            error_detail=None,
            idempotency_key="lease-wait-request",
            lease_owner="previous-worker",
            lease_expires_at=now - timedelta(seconds=1),
            created_at=now,
            updated_at=now,
        )
        chunk = ChunkRecord(
            id="leased-chunk",
            job_id=job.id,
            seq=0,
            status=ChunkStatus.INFLIGHT,
            lease_owner="previous-worker",
            lease_expires_at=now + timedelta(seconds=2.5),
            created_at=now,
        )
        links = [
            ChunkBlockRecord(chunk_id=chunk.id, block_id=block.id, seq_in_chunk=index)
            for index, block in enumerate(document_ir.blocks)
        ]
        await SqliteJobExecutionRepository(connection).create_job_with_chunks(
            job,
            [chunk],
            links,
        )

        provider = TrackingFakeProvider(settings)
        worker = ClaimLoop(
            settings,
            job_repo,
            cache_repo,
            document_repo,
            provider,
            ModelCostCalculator(),
            formats,
            storage,
            persistence=persistence,
            shutdown_event=shutdown,
        )
        worker_task = asyncio.create_task(worker.run())
        await asyncio.sleep(1.2)
        assert provider.calls == 0
        async with persistence.read():
            current_job = await job_repo.get_job(job.id)
            current_chunks = await job_repo.get_pending_chunks(job.id)
        assert current_job is not None and current_job.status is JobStatus.RUNNING
        assert current_chunks == []

        async with asyncio.timeout(10):
            while True:
                async with persistence.read():
                    completed = await job_repo.get_job(job.id)
                if completed is not None and completed.status is JobStatus.DONE:
                    break
                await asyncio.sleep(0.01)
        shutdown.set()
        await worker_task
        worker_task = None
        assert provider.calls == 1
    finally:
        if worker_task is not None and not worker_task.done():
            shutdown.set()
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)
        await connection.close()


async def test_claim_loop_recovers_assembling_and_persists_safe_render_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = Mock()
    monkeypatch.setattr(claim_loop, "_logger", logger)
    for scenario in ("assembling", "render_failure"):
        scenario_path = tmp_path / scenario
        scenario_path.mkdir()
        worker_id = f"{scenario}-worker"
        settings = Settings(
            database_path=scenario_path / "worker.db",
            upload_storage_path=scenario_path / "uploads",
            output_storage_path=scenario_path / "outputs",
            worker_id=worker_id,
            heartbeat_interval_seconds=1,
            job_lease_seconds=4,
            chunk_lease_seconds=4,
            max_chunk_concurrency=1,
        )
        connection = await SqliteConnectionFactory(settings.database_path).create()
        shutdown = asyncio.Event()
        worker_task: asyncio.Task[None] | None = None
        try:
            storage = FilesystemStorage(
                settings.upload_storage_path,
                settings.output_storage_path,
            )
            document_repo = SqliteDocumentRepository(connection)
            cache_repo = SqliteTranslationCacheRepository(connection)
            persistence = WorkerPersistence(connection)
            job_repo = SqliteJobExecutionRepository(connection, worker_id=worker_id)
            registered_formats = FormatRegistry()
            registered_formats.register("docx", DocxExtractor(), DocxRenderer())
            registered_formats.register("pdf", PdfExtractor(), PdfRenderer())
            sample_path = Path(__file__).parents[2] / "samples" / "sample_en.docx"
            content = await asyncio.to_thread(sample_path.read_bytes)
            upload_path = await storage.save_upload("recovery-document", content, sample_path.name)
            resolved = await registered_formats.resolve(upload_path)
            assert resolved is not None
            document_ir = await resolved[0].extract(upload_path, "recovery-document")
            async with transaction(connection):
                await document_repo.create_document(
                    "recovery-document",
                    document_ir.filename,
                    "docx",
                    document_ir.size_bytes,
                    str(upload_path),
                    document_ir.page_count,
                )
                await document_repo.create_blocks("recovery-document", document_ir.blocks)

            now = datetime.now(UTC)
            recovering = scenario == "assembling"
            job = JobRecord(
                id=f"{scenario}-job",
                document_id="recovery-document",
                batch_id=f"{scenario}-batch",
                target_language="de",
                status=JobStatus.ASSEMBLING if recovering else JobStatus.QUEUED,
                total_chunks=1,
                done_chunks=1 if recovering else 0,
                model="gpt-4o-mini",
                prompt_version="recovery-v1",
                tokens_in=0,
                tokens_out=0,
                cost_usd=0,
                error_code=None,
                error_detail=None,
                idempotency_key=f"{scenario}-request",
                lease_owner="stopped-worker" if recovering else None,
                lease_expires_at=now - timedelta(seconds=1) if recovering else None,
                created_at=now,
                updated_at=now,
            )
            chunk = ChunkRecord(
                id=f"{scenario}-chunk",
                job_id=job.id,
                seq=0,
                status=ChunkStatus.DONE if recovering else ChunkStatus.PENDING,
                lease_owner=None,
                lease_expires_at=None,
                created_at=now,
            )
            links = [
                ChunkBlockRecord(chunk_id=chunk.id, block_id=block.id, seq_in_chunk=index)
                for index, block in enumerate(document_ir.blocks)
            ]
            await SqliteJobExecutionRepository(connection).create_job_with_chunks(
                job,
                [chunk],
                links,
            )
            if recovering:
                async with transaction(connection):
                    for block in document_ir.blocks:
                        await cache_repo.save_block_translation(
                            translation_key(job),
                            block.id,
                            f"[de] {block.source_text}",
                        )

            worker_formats = registered_formats if recovering else FormatRegistry()
            provider = TrackingFakeProvider(settings)
            worker = ClaimLoop(
                settings,
                job_repo,
                cache_repo,
                document_repo,
                provider,
                ModelCostCalculator(),
                worker_formats,
                storage,
                persistence=persistence,
                shutdown_event=shutdown,
            )
            worker_task = asyncio.create_task(worker.run())
            async with asyncio.timeout(10):
                while True:
                    async with persistence.read():
                        finished = await job_repo.get_job(job.id)
                    if finished is not None and finished.status in {
                        JobStatus.DONE,
                        JobStatus.FAILED,
                    }:
                        break
                    await asyncio.sleep(0.01)
            shutdown.set()
            await worker_task
            worker_task = None

            assert finished is not None
            if recovering:
                assert finished.status is JobStatus.DONE
                assert provider.calls == 0
                await storage.get_output_path(job.id)
            else:
                assert finished.status is JobStatus.FAILED
                assert finished.error_code == ErrorCode.RENDER_FAILED.value
                assert finished.error_detail == DocumentError(ErrorCode.RENDER_FAILED).message
                assert provider.calls == job.total_chunks
                logger.error.assert_called_once_with(
                    "worker_render_failed",
                    job_id=job.id,
                    document_id=job.document_id,
                    error_code=ErrorCode.RENDER_FAILED.value,
                )
        finally:
            if worker_task is not None and not worker_task.done():
                shutdown.set()
                worker_task.cancel()
                await asyncio.gather(worker_task, return_exceptions=True)
            await connection.close()
