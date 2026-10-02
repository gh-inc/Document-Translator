"""Real-format worker integrations using only the deterministic fake provider."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from docx import Document as OpenDocument

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


class TrackingFakeProvider(FakeProvider):
    def __init__(
        self,
        settings: Settings,
        connection,
        shutdown: asyncio.Event,
        *,
        delay: float = 0.0,
        assert_no_transaction: bool = False,
    ) -> None:
        super().__init__(settings=settings, fail_rate=0, latency_ms=0)
        self.connection = connection
        self.shutdown = shutdown
        self.delay = delay
        self.assert_no_transaction = assert_no_transaction
        self.active = 0
        self.max_active = 0
        self.calls: list[tuple[str, ...]] = []

    async def translate_chunk(self, request):
        if self.assert_no_transaction:
            assert not self.connection.in_transaction
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.calls.append(tuple(block.id for block in request.blocks))
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            result = await super().translate_chunk(request)
            if self.assert_no_transaction:
                assert not self.connection.in_transaction
            self.shutdown.set()
            return result
        finally:
            self.active -= 1


async def _wait_for_terminal(
    persistence: WorkerPersistence,
    job_repo: SqliteJobExecutionRepository,
    job_id: str,
) -> JobRecord:
    async def read_job() -> JobRecord | None:
        async with persistence.read():
            return await job_repo.get_job(job_id)

    async with asyncio.timeout(20):
        while True:
            job = await read_job()
            if job is not None and job.status in {
                JobStatus.DONE,
                JobStatus.COMPLETED_WITH_ERRORS,
                JobStatus.FAILED,
            }:
                return job
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("extension", ["pdf", "docx"])
async def test_worker_translates_and_assembles_real_samples(
    tmp_path: Path,
    extension: str,
) -> None:
    settings = Settings(
        database_path=tmp_path / "worker.db",
        upload_storage_path=tmp_path / "uploads",
        output_storage_path=tmp_path / "outputs",
        worker_id="integration-worker",
        heartbeat_interval_seconds=1,
        job_lease_seconds=3,
        chunk_lease_seconds=3,
        max_chunk_concurrency=3 if extension == "pdf" else 1,
        max_chunk_attempts=2,
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
        formats.register("pdf", PdfExtractor(), PdfRenderer())
        formats.register("docx", DocxExtractor(), DocxRenderer())

        document_id = "sample-document"
        sample_path = Path(__file__).parents[2] / "samples" / f"sample_en.{extension}"
        content = await asyncio.to_thread(sample_path.read_bytes)
        upload_path = await storage.save_upload(document_id, content, sample_path.name)
        adapter_pair = await formats.resolve(upload_path)
        assert adapter_pair is not None
        document_ir = await adapter_pair[0].extract(upload_path, document_id)
        async with transaction(connection):
            await document_repo.create_document(
                document_id,
                document_ir.filename,
                extension,
                document_ir.size_bytes,
                str(upload_path),
                document_ir.page_count,
            )
            await document_repo.create_blocks(document_id, document_ir.blocks)

        now = datetime.now(UTC)
        job = JobRecord(
            id="sample-job",
            document_id=document_id,
            batch_id="sample-batch",
            target_language="de",
            status=JobStatus.QUEUED,
            total_chunks=len(document_ir.blocks),
            done_chunks=0,
            model="gpt-4o-mini",
            prompt_version="worker-test-v1",
            tokens_in=0,
            tokens_out=0,
            cost_usd=0,
            error_code=None,
            error_detail=None,
            idempotency_key="sample-request",
            lease_owner=None,
            lease_expires_at=None,
            created_at=now,
            updated_at=now,
        )
        chunks = [
            ChunkRecord(
                id=f"sample-chunk-{index}",
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

        queued_job = job.model_copy(
            update={
                "id": "sample-job-next",
                "batch_id": "sample-batch-next",
                "idempotency_key": "sample-request-next",
                "created_at": now + timedelta(seconds=1),
            }
        )
        queued_chunks = [
            chunk.model_copy(update={"id": f"next-{chunk.id}", "job_id": queued_job.id})
            for chunk in chunks
        ]
        queued_links = [
            ChunkBlockRecord(
                chunk_id=chunk.id,
                block_id=block.id,
                seq_in_chunk=0,
            )
            for chunk, block in zip(queued_chunks, document_ir.blocks, strict=True)
        ]
        await SqliteJobExecutionRepository(connection).create_job_with_chunks(
            queued_job,
            queued_chunks,
            queued_links,
        )

        provider = TrackingFakeProvider(
            settings,
            connection,
            shutdown,
            delay=0.02,
            assert_no_transaction=extension == "docx",
        )
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
        result = await _wait_for_terminal(persistence, job_repo, job.id)
        shutdown.set()
        await worker_task
        worker_task = None

        assert result.status is JobStatus.DONE
        assert result.done_chunks == result.total_chunks == len(document_ir.blocks)
        assert len(provider.calls) == len(document_ir.blocks)
        if extension == "pdf":
            assert 1 < provider.max_active <= settings.max_chunk_concurrency
        else:
            assert provider.max_active == 1
        async with persistence.read():
            next_job = await job_repo.get_job(queued_job.id)
        assert next_job is not None and next_job.status is JobStatus.QUEUED
        output_path = await storage.get_output_path(job.id)
        assert output_path.suffix == f".{extension}"
        if extension == "docx":
            output = await asyncio.to_thread(OpenDocument, str(output_path))
            assert any(paragraph.text.startswith("[de]") for paragraph in output.paragraphs)
        else:
            output_ir = await PdfExtractor().extract(output_path, "translated-sample")
            assert any(block.source_text.startswith("[de]") for block in output_ir.blocks)
    finally:
        if worker_task is not None and not worker_task.done():
            shutdown.set()
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)
        await connection.close()
