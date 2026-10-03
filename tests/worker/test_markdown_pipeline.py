"""Persisted Markdown translation tests with restart-safe bypass classification."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.adapters.formats.markdown import MarkdownExtractor, MarkdownRenderer
from app.adapters.formats.registry import FormatRegistry
from app.adapters.llm.fake_provider import FakeProvider
from app.adapters.llm.pricing import ModelCostCalculator
from app.adapters.persistence.api import ApiJobExecutionRepository, ApiPersistence
from app.adapters.persistence.database import SqliteConnectionFactory, transaction
from app.adapters.persistence.repositories import (
    SqliteDocumentRepository,
    SqliteJobExecutionRepository,
    SqliteTranslationCacheRepository,
)
from app.adapters.persistence.worker import WorkerPersistence
from app.adapters.storage.filesystem import FilesystemStorage
from app.config import Settings
from app.core.models import DocumentStatus, JobStatus, TranslationPlan
from app.core.services.job_service import JobService
from app.worker.claim_loop import ClaimLoop


class _TrackingFakeProvider(FakeProvider):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings=settings, fail_rate=0, latency_ms=0)
        self.requests: list[Any] = []

    async def translate_chunk(self, request: Any) -> Any:
        self.requests.append(request)
        return await super().translate_chunk(request)


@pytest.fixture
async def pipeline_context(tmp_path: Path):
    settings = Settings(
        database_path=tmp_path / "markdown.db",
        upload_storage_path=tmp_path / "uploads",
        output_storage_path=tmp_path / "outputs",
        worker_id="markdown-worker",
        heartbeat_interval_seconds=1,
        job_lease_seconds=5,
        chunk_lease_seconds=5,
        max_chunk_concurrency=1,
    )
    connection = await SqliteConnectionFactory(settings.database_path).create()
    yield settings, connection
    await connection.close()


async def _translate_markdown(
    settings: Settings,
    connection,
    tmp_path: Path,
    document_id: str,
    source: str,
    provider: _TrackingFakeProvider,
) -> tuple[Any, list[str], set[str], list[str]]:
    source_path = tmp_path / f"{document_id}.md"
    await asyncio.to_thread(source_path.write_text, source, encoding="utf-8")
    storage = FilesystemStorage(settings.upload_storage_path, settings.output_storage_path)
    upload_path = await storage.save_upload(
        document_id, await asyncio.to_thread(source_path.read_bytes), source_path.name
    )

    extraction_registry = FormatRegistry()
    extraction_registry.register("md", MarkdownExtractor(), MarkdownRenderer())
    resolved = await extraction_registry.resolve(upload_path)
    assert resolved is not None
    document_ir = await resolved[0].extract(upload_path, document_id)

    document_repo = SqliteDocumentRepository(connection)
    api_job_repo = ApiJobExecutionRepository(connection)
    cache_repo = SqliteTranslationCacheRepository(connection)
    api_persistence = ApiPersistence(connection)
    async with transaction(connection):
        await document_repo.create_document(
            document_id,
            document_ir.filename,
            "md",
            document_ir.size_bytes,
            str(upload_path),
            document_ir.page_count,
        )
        await document_repo.create_blocks(document_id, document_ir.blocks)
        await document_repo.update_document_status(document_id, DocumentStatus.EXTRACTED)
        await document_repo.save_analysis(
            document_id,
            TranslationPlan(source_language="en", domain="general", register="neutral"),
        )

    # A new registry/extractor instance classifies the persisted metadata. It
    # cannot rely on extraction-time in-memory skip_block_ids state.
    service_registry = FormatRegistry()
    service_registry.register("md", MarkdownExtractor(), MarkdownRenderer())

    async def resolve_skip_ids(document, blocks):
        return service_registry.get_skip_block_ids(document.format, blocks)

    jobs = JobService(
        document_repo,
        api_job_repo,
        cache_repo,
        ModelCostCalculator(),
        persistence=api_persistence,
        settings=settings,
        skip_block_ids_resolver=resolve_skip_ids,
    )
    job = (await jobs.create_jobs(document_id, ["de"], f"{document_id}-request"))[0]

    worker_formats = FormatRegistry()
    worker_formats.register("md", MarkdownExtractor(), MarkdownRenderer())
    worker_persistence = WorkerPersistence(connection)
    shutdown = asyncio.Event()
    worker_jobs = ClaimLoop(
        settings,
        SqliteJobExecutionRepository(connection, worker_id=settings.worker_id),
        cache_repo,
        document_repo,
        provider,
        ModelCostCalculator(),
        worker_formats,
        storage,
        persistence=worker_persistence,
        shutdown_event=shutdown,
    )
    worker_task = asyncio.create_task(worker_jobs.run())
    try:
        async with asyncio.timeout(10):
            while True:
                async with worker_persistence.read():
                    completed = await api_job_repo.get_job(job.id)
                if completed is not None and completed.status in {
                    JobStatus.DONE,
                    JobStatus.COMPLETED_WITH_ERRORS,
                    JobStatus.FAILED,
                }:
                    break
                await asyncio.sleep(0.01)
    finally:
        shutdown.set()
        await asyncio.wait_for(worker_task, timeout=3)

    assert completed is not None
    assert completed.status is JobStatus.DONE
    async with connection.execute(
        "SELECT chunk_blocks.block_id FROM chunk_blocks "
        "JOIN chunks ON chunks.id = chunk_blocks.chunk_id "
        "WHERE chunks.job_id = ? ORDER BY chunks.seq, chunk_blocks.seq_in_chunk",
        (job.id,),
    ) as cursor:
        linked_ids = [str(row[0]) for row in await cursor.fetchall()]
    async with connection.execute(
        "SELECT blocks.id FROM blocks "
        "JOIN block_translations ON block_translations.source_hash = blocks.source_hash "
        "WHERE blocks.document_id = ? ORDER BY blocks.seq",
        (document_id,),
    ) as cursor:
        cached_ids = [str(row[0]) for row in await cursor.fetchall()]
    async with connection.execute(
        "SELECT id FROM blocks WHERE document_id = ? ORDER BY seq", (document_id,)
    ) as cursor:
        all_ids = [str(row[0]) for row in await cursor.fetchall()]

    return completed, linked_ids, set(cached_ids), all_ids


async def test_markdown_empty_cells_never_link_or_cache_and_repeated_text_is_reused(
    pipeline_context,
    tmp_path: Path,
) -> None:
    settings, connection = pipeline_context
    provider = _TrackingFakeProvider(settings)
    baseline, baseline_links, baseline_cache, baseline_ids = await _translate_markdown(
        settings,
        connection,
        tmp_path,
        "markdown-baseline",
        "| Heading | Alpha narrative. | Beta narrative. |\n| --- | --- | --- |\n",
        provider,
    )
    merged, merged_links, merged_cache, merged_ids = await _translate_markdown(
        settings,
        connection,
        tmp_path,
        "markdown-merged",
        "| Heading | Alpha narrative. | Beta narrative. | | |\n| --- | --- | --- | --- | --- |\n",
        provider,
    )

    assert baseline.status is merged.status is JobStatus.DONE
    assert baseline.total_chunks == merged.total_chunks == 1
    assert len(baseline_ids) == len(baseline_links) == len(baseline_cache) == 3
    assert len(merged_ids) == 5
    assert set(merged_links) == set(merged_cache)
    assert len(merged_links) == 3
    assert len(merged_ids) - len(merged_links) == 2
    assert len(provider.requests) == 1
    assert len(provider.requests[0].blocks) == 3
    assert baseline.tokens_in > 0 and merged.tokens_in == 0
    assert baseline.tokens_out > 0 and merged.tokens_out == 0
    assert baseline.cost_usd > 0 and merged.cost_usd == 0
    assert (merged.cache_hit_blocks, merged.cache_miss_blocks) == (3, 0)


async def test_markdown_document_with_only_empty_cells_has_zero_chunks_and_completes(
    pipeline_context,
    tmp_path: Path,
) -> None:
    settings, connection = pipeline_context
    provider = _TrackingFakeProvider(settings)
    completed, linked_ids, cached_ids, block_ids = await _translate_markdown(
        settings,
        connection,
        tmp_path,
        "markdown-structural-only",
        "| | |\n| --- | --- |\n",
        provider,
    )

    assert completed.total_chunks == 0
    assert completed.status is JobStatus.DONE
    assert block_ids
    assert linked_ids == []
    assert cached_ids == set()
    assert provider.requests == []
    assert completed.tokens_in == completed.tokens_out == 0
    assert completed.cost_usd == 0
    assert (completed.cache_hit_blocks, completed.cache_miss_blocks) == (0, 0)
