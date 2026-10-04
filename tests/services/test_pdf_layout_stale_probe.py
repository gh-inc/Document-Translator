"""Isolated clean-worker reproduction for the PDF layout investigation."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import pymupdf
import pytest
import structlog
import tiktoken
from structlog.testing import capture_logs

from app.adapters.formats import pdf as pdf_adapter
from app.adapters.formats.pdf import PdfExtractor, PdfRenderer
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
from app.core.services.cache_keys import translation_key
from app.core.services.job_service import JobService
from app.worker import assembly as assembly_adapter
from app.worker import claim_loop as claim_loop_adapter
from app.worker import translation_loop as translation_loop_adapter
from app.worker.claim_loop import ClaimLoop

SAMPLE = Path(__file__).resolve().parents[2] / "samples" / "platon-gliph.pdf"
CACHE_SNAPSHOT = (
    Path(__file__).resolve().parents[1]
    / "adapters"
    / "formats"
    / "fixtures"
    / "platon-gliph-en-cache-snapshot.json"
)


class _CharacterEncoding:
    def encode(self, text: str, *, disallowed_special: tuple[str, ...] = ()) -> list[int]:
        del disallowed_special
        return [1] * len(text)


class NoCallFakeProvider(FakeProvider):
    """Fake provider guard: a complete cache hit must make zero calls."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings=settings, fail_rate=0, latency_ms=0)
        self.calls = 0

    async def translate_chunk(self, request: Any):
        self.calls += 1
        raise AssertionError("isolated cache-hit reproduction unexpectedly called provider")


def _read_pdf_text(path: Path) -> tuple[int, str]:
    with pymupdf.open(path) as document:
        return len(document), "\n".join(page.get_text() for page in document)


async def test_fresh_pdf_worker_renders_all_cached_blocks_without_provider_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise extraction, persistent cache reads, worker assembly and PDF render."""
    monkeypatch.setattr(tiktoken, "encoding_for_model", lambda _model: _CharacterEncoding())
    content = await asyncio.to_thread(SAMPLE.read_bytes)
    document_id = hashlib.sha256(content).hexdigest()
    snapshot = json.loads(await asyncio.to_thread(CACHE_SNAPSHOT.read_text, encoding="utf-8"))
    provenance = snapshot["provenance"]
    assert provenance["source_document_id"] == document_id
    assert provenance["source_filename"] == SAMPLE.name
    assert provenance["target_language"] == "en"
    settings = Settings(
        database_path=tmp_path / "isolated-pdf.db",
        upload_storage_path=tmp_path / "uploads",
        output_storage_path=tmp_path / "outputs",
        worker_id="isolated-pdf-worker",
        openai_model=provenance["model"],
        heartbeat_interval_seconds=1,
        job_lease_seconds=5,
        chunk_lease_seconds=5,
        max_chunk_concurrency=2,
    )
    connection = await SqliteConnectionFactory(settings.database_path).create()
    storage = FilesystemStorage(settings.upload_storage_path, settings.output_storage_path)
    document_repo = SqliteDocumentRepository(connection)
    api_job_repo = ApiJobExecutionRepository(connection)
    cache_repo = SqliteTranslationCacheRepository(connection)
    api_persistence = ApiPersistence(connection)
    worker_persistence = WorkerPersistence(connection)
    formats = FormatRegistry()
    formats.register("pdf", PdfExtractor(), PdfRenderer())
    shutdown = asyncio.Event()
    worker_task: asyncio.Task[None] | None = None
    log_path = tmp_path / "worker.log"

    try:
        upload_path = await storage.save_upload(document_id, content, SAMPLE.name)
        extractor = PdfExtractor()
        extracted = await extractor.extract(upload_path, document_id)
        plan = TranslationPlan(
            source_language="ru",
            domain="philosophy",
            register="neutral",
        )
        translated_by_id = {
            block.id: snapshot["translations_by_seq"][str(block.seq)] for block in extracted.blocks
        }
        translations_by_hash: dict[str, str] = {}
        for block in extracted.blocks:
            translation = translated_by_id[block.id]
            previous = translations_by_hash.setdefault(block.source_hash, translation)
            assert previous == translation
        async with transaction(connection):
            await document_repo.create_document(
                document_id,
                extracted.filename,
                extracted.format,
                extracted.size_bytes,
                str(upload_path),
                extracted.page_count,
            )
            await document_repo.create_blocks(document_id, extracted.blocks)
            await document_repo.update_document_status(document_id, DocumentStatus.EXTRACTED)
            await document_repo.save_analysis(document_id, plan)

        job_service = JobService(
            document_repo,
            api_job_repo,
            cache_repo,
            ModelCostCalculator(),
            persistence=api_persistence,
            settings=settings,
        )
        job = (
            await job_service.create_jobs(
                document_id,
                [provenance["target_language"]],
                "isolated-pdf-request",
            )
        )[0]
        assert job.model == provenance["model"]
        key = translation_key(
            provenance["target_language"], job.model, job.prompt_version, job.glossary, plan
        )
        async with transaction(connection):
            for source_hash, translated_text in translations_by_hash.items():
                await cache_repo.save_block_translation(key, source_hash, translated_text)

        provider = NoCallFakeProvider(settings)
        worker = ClaimLoop(
            settings,
            SqliteJobExecutionRepository(connection, worker_id=settings.worker_id),
            cache_repo,
            document_repo,
            provider,
            ModelCostCalculator(),
            formats,
            storage,
            persistence=worker_persistence,
            shutdown_event=shutdown,
        )

        # Create the diagnostic destination and begin log capture before the
        # isolated worker starts claiming the queued job.
        await asyncio.to_thread(log_path.touch)
        with capture_logs() as logs:
            # Some earlier tests may have initialized cached BoundLoggers before
            # capture_logs changed the processor chain. Rebind the two events
            # this probe measures while capture is active.
            monkeypatch.setattr(pdf_adapter, "_logger", structlog.get_logger(pdf_adapter.__name__))
            monkeypatch.setattr(
                assembly_adapter,
                "_logger",
                structlog.get_logger(assembly_adapter.__name__),
            )
            monkeypatch.setattr(
                claim_loop_adapter,
                "_logger",
                structlog.get_logger(claim_loop_adapter.__name__),
            )
            monkeypatch.setattr(
                translation_loop_adapter,
                "logger",
                structlog.get_logger(translation_loop_adapter.__name__),
            )
            worker_task = asyncio.create_task(worker.run())
            async with asyncio.timeout(30):
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
            shutdown.set()
            await worker_task
            worker_task = None
        await asyncio.to_thread(log_path.write_text, json.dumps(logs, default=str), "utf-8")

        assert completed is not None
        assert completed.status is JobStatus.COMPLETED_WITH_ERRORS  # DT-77 glyph fallback
        assert completed.done_chunks == completed.total_chunks == job.total_chunks
        assert completed.cache_hit_blocks == len(extracted.blocks)
        assert completed.cache_miss_blocks == 0
        assert provider.calls == 0

        output_path = await storage.get_output_path(job.id)
        page_count, output_text = await asyncio.to_thread(_read_pdf_text, output_path)
        normalized_output = " ".join(output_text.split())
        assert page_count == extracted.page_count + 42 == 45
        degraded = next(entry for entry in logs if entry.get("event") == "worker_render_degraded")
        fallback = next(entry for entry in logs if entry.get("event") == "pdf_render_completed")
        assert degraded.get("degraded_block_count") == 1
        assert fallback.get("fallback_count") == fallback.get("fallback_pages_count") == 42
        for block in extracted.blocks:
            if block.id not in degraded.get("degraded_block_ids", []):
                assert " ".join(translated_by_id[block.id].split()) in normalized_output
        assert any(entry.get("event") == "worker_job_claimed" for entry in logs)
        assert any(entry.get("event") == "translation_chunk_cache_hit" for entry in logs)
        assert any(entry.get("event") == "pdf_render_completed" for entry in logs)
        assert log_path.is_file() and log_path.stat().st_size > 0
        print(
            "isolated PDF worker: "
            f"blocks={len(extracted.blocks)}, cache_hits={completed.cache_hit_blocks}, "
            f"cache_misses={completed.cache_miss_blocks}, provider_calls={provider.calls}, "
            f"pages={extracted.page_count}->{page_count}, fallback_pages=42, "
            f"degraded_blocks=1, logs={log_path}"
        )
    finally:
        shutdown.set()
        if worker_task is not None:
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)
        await connection.close()
