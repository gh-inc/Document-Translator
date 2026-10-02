"""Job creation and retry behavior across the API/persistence boundary."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import tiktoken

from app.adapters.llm.fake_provider import FakeProvider
from app.adapters.llm.pricing import ModelCostCalculator
from app.adapters.persistence.api import ApiJobExecutionRepository, ApiPersistence
from app.adapters.persistence.database import SqliteConnectionFactory, transaction
from app.adapters.persistence.repositories import (
    SqliteDocumentRepository,
    SqliteTranslationCacheRepository,
)
from app.adapters.persistence.worker import WorkerPersistence
from app.config import Settings
from app.core.errors import ErrorCode, ProviderError, ServiceError
from app.core.models import (
    AttemptOutcome,
    Block,
    ChunkAttemptRecord,
    DocumentStatus,
    JobError,
    JobRecord,
    JobStatus,
    TranslationPlan,
)
from app.core.ports import CostCalculator
from app.core.services.job_service import JobService
from app.worker.translation_loop import TranslationLoop


class _CharacterEncoding:
    def encode(self, text: str, *, disallowed_special: tuple[str, ...] = ()) -> list[int]:
        del disallowed_special
        return [1] * len(text)


class _TrackingFakeProvider(FakeProvider):
    def __init__(self) -> None:
        super().__init__(fail_rate=1.0, fail_mode="429")
        self.requests: list[Any] = []

    async def translate_chunk(self, request: Any) -> Any:
        self.requests.append(request)
        return await super().translate_chunk(request)


class _FlatCostCalculator(CostCalculator):
    def estimate(self, model: str, tokens_in: int, tokens_out: int) -> float:
        del model, tokens_in, tokens_out
        return 1.0


@pytest.fixture
async def job_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[dict[str, Any]]:
    monkeypatch.setattr(tiktoken, "encoding_for_model", lambda _model: _CharacterEncoding())
    database_path = tmp_path / "jobs.db"
    connection = await SqliteConnectionFactory(database_path).create()
    document_repo = SqliteDocumentRepository(connection)
    job_repo = ApiJobExecutionRepository(connection)
    cache_repo = SqliteTranslationCacheRepository(connection)
    persistence = ApiPersistence(connection)
    settings = Settings(
        database_path=database_path,
        upload_storage_path=tmp_path / "uploads",
        output_storage_path=tmp_path / "out",
        max_chunk_attempts=4,
        max_cost_per_job_usd=2.0,
    )
    blocks = [
        Block(
            id=f"block-{seq}",
            seq=seq,
            source_text="x" * 400,
            source_hash=f"source-{seq}",
            format_metadata={"arbitrary": object.__name__},
        )
        for seq in range(4)
    ]
    async with transaction(connection):
        await document_repo.create_document(
            "doc-1", "source.pdf", "pdf", 100, "/uploads/doc-1/source.pdf", 1
        )
        await document_repo.update_document_status("doc-1", DocumentStatus.EXTRACTED)
        await document_repo.create_blocks("doc-1", blocks)
        await document_repo.save_analysis(
            "doc-1",
            TranslationPlan(
                source_language="en",
                domain="business",
                register="neutral",
                terms=["Document Translator"],
            ),
        )
    service = JobService(
        document_repo,
        job_repo,
        cache_repo,
        ModelCostCalculator(),
        persistence=persistence,
        settings=settings,
    )
    try:
        yield {
            "database_path": database_path,
            "connection": connection,
            "document_repo": document_repo,
            "job_repo": job_repo,
            "cache_repo": cache_repo,
            "persistence": persistence,
            "settings": settings,
            "blocks": blocks,
            "service": service,
        }
    finally:
        await connection.close()


async def test_create_jobs_chunks_whole_blocks_and_normalizes_idempotent_batch(
    job_context: dict[str, Any],
) -> None:
    service: JobService = job_context["service"]

    created = await service.create_jobs("doc-1", ["French", " french ", "de"], "request-1")
    repeated = await service.create_jobs("doc-1", ["DE", "FRENCH"], "request-1")

    assert [job.target_language for job in created] == ["french", "de"]
    assert [job.id for job in repeated] == [created[1].id, created[0].id]
    assert len({job.batch_id for job in created}) == 1
    assert created[0].batch_id.startswith(JobService._digest("request-1"))
    assert all(job.total_chunks == 2 for job in created)
    assert all(job.glossary == {"Document Translator": "Document Translator"} for job in created)
    assert all(job.idempotency_key != created[0].idempotency_key for job in created[1:])

    async with job_context["connection"].execute(
        "SELECT chunk_blocks.chunk_id, chunk_blocks.block_id, chunk_blocks.seq_in_chunk "
        "FROM chunk_blocks JOIN chunks ON chunks.id = chunk_blocks.chunk_id "
        "WHERE chunks.job_id = ? ORDER BY chunks.seq, chunk_blocks.seq_in_chunk",
        (created[0].id,),
    ) as cursor:
        rows = await cursor.fetchall()
    assert len(rows) == 4
    assert [str(row[1]) for row in rows] == [block.id for block in job_context["blocks"]]
    assert [int(row[2]) for row in rows] == [0, 1, 0, 1]


async def test_same_key_with_different_document_or_language_set_conflicts(
    job_context: dict[str, Any],
) -> None:
    service: JobService = job_context["service"]
    await service.create_jobs("doc-1", ["de", "fr"], "request-2")

    with pytest.raises(ServiceError) as different_languages:
        await service.create_jobs("doc-1", ["de"], "request-2")
    assert different_languages.value.error_code is ErrorCode.CONFLICT
    assert different_languages.value.status_code == 409

    with pytest.raises(ServiceError) as different_document:
        await service.create_jobs("doc-other", ["de", "fr"], "request-2")
    assert different_document.value.error_code is ErrorCode.CONFLICT


async def test_incomplete_aggregate_batch_can_resume_after_a_write_failure(
    job_context: dict[str, Any],
) -> None:
    service: JobService = job_context["service"]
    job_repo = job_context["job_repo"]
    original = job_repo.create_job_with_chunks
    calls = 0

    async def fail_second(job: JobRecord, chunks: list[Any], links: list[Any]) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected second-language write failure")
        await original(job, chunks, links)

    job_repo.create_job_with_chunks = fail_second
    with pytest.raises(ServiceError) as failed:
        await service.create_jobs("doc-1", ["de", "fr"], "request-3")
    assert failed.value.error_code is ErrorCode.CONFLICT

    job_repo.create_job_with_chunks = original
    recovered = await service.create_jobs("doc-1", ["de", "fr"], "request-3")
    assert {job.target_language for job in recovered} == {"de", "fr"}


async def test_retry_preserves_history_and_restores_cap_after_database_restart(
    job_context: dict[str, Any],
) -> None:
    service: JobService = job_context["service"]
    connection = job_context["connection"]
    job_repo = job_context["job_repo"]
    cache_repo = job_context["cache_repo"]
    blocks: list[Block] = job_context["blocks"]
    job = (await service.create_jobs("doc-1", ["de"], "request-4"))[0]
    async with connection.execute(
        "SELECT chunks.id FROM chunks WHERE chunks.job_id = ? ORDER BY chunks.seq",
        (job.id,),
    ) as cursor:
        chunk_rows = await cursor.fetchall()
    first_chunk_id, failed_chunk_id = (str(row[0]) for row in chunk_rows)
    key = service._translation_key(job)

    async with transaction(connection):
        await cache_repo.save_block_translation(key, blocks[0].id, "cached translated paragraph")
        await cache_repo.save_block_translation(key, blocks[1].id, "another cached paragraph")
        for attempt_no in range(1, 5):
            await job_repo.record_chunk_attempt(
                ChunkAttemptRecord(
                    id=f"previous-attempt-{attempt_no}",
                    chunk_id=failed_chunk_id,
                    attempt_no=attempt_no,
                    tokens_in=0,
                    tokens_out=0,
                    cost_usd=0.1,
                    latency_ms=1,
                    outcome=AttemptOutcome.RETRYABLE_ERROR,
                    error_detail="previous retry",
                    created_at=datetime.now(UTC),
                )
            )
        await job_repo.complete_job(
            job.id,
            JobStatus.COMPLETED_WITH_ERRORS,
            JobError(
                error_code=ErrorCode.PROVIDER_TIMEOUT.value,
                message=ProviderError(ErrorCode.PROVIDER_TIMEOUT).message,
                retryable=True,
            ),
        )

    retried = await service.retry_job(job.id, raised_cost_cap_usd=10.0)
    assert retried is not None and retried.status is JobStatus.QUEUED
    assert retried.error_code is None
    policy = json.loads(retried.error_detail or "")["_internal_retry_policy"]
    assert policy["max_cost_per_job_usd"] == 10.0
    assert policy["attempt_baselines"] == {failed_chunk_id: 4}
    assert retried.cost_usd == pytest.approx(0.4)

    async with connection.execute(
        "SELECT id, status FROM chunks WHERE job_id = ? ORDER BY seq",
        (job.id,),
    ) as cursor:
        chunk_states = await cursor.fetchall()
    assert [(str(row[0]), str(row[1])) for row in chunk_states] == [
        (first_chunk_id, "done"),
        (failed_chunk_id, "pending"),
    ]
    assert (
        await cache_repo.get_block_translation(key, blocks[0].id) == "cached translated paragraph"
    )
    assert await cache_repo.get_block_translation(key, blocks[1].id) == "another cached paragraph"

    database_path: Path = job_context["database_path"]
    await connection.close()
    restarted = await SqliteConnectionFactory(database_path).create()
    worker_repo = ApiJobExecutionRepository(restarted)
    settings: Settings = job_context["settings"].model_copy(update={"worker_id": "retry-worker"})
    try:
        async with transaction(restarted):
            claimed_job = await worker_repo.claim_job(
                settings.worker_id, datetime.now(UTC) + timedelta(minutes=2)
            )
        assert claimed_job is not None
        assert (
            json.loads(claimed_job.error_detail or "")["_internal_retry_policy"][
                "max_cost_per_job_usd"
            ]
            == 10.0
        )
        async with transaction(restarted):
            claimed_chunk = await worker_repo.claim_chunk(
                claimed_job.id,
                settings.worker_id,
                datetime.now(UTC) + timedelta(minutes=2),
            )
        assert claimed_chunk is not None and claimed_chunk.id == failed_chunk_id
        persistence = WorkerPersistence(restarted)
        provider = _TrackingFakeProvider()
        loop = TranslationLoop(
            settings,
            worker_repo,
            SqliteTranslationCacheRepository(restarted),
            provider,
            _FlatCostCalculator(),
            persistence=persistence,
        )
        all_blocks = await SqliteDocumentRepository(restarted).get_blocks(job.document_id)
        async with persistence.read():
            retry_blocks = await persistence.get_chunk_blocks(failed_chunk_id)
        await loop.process_chunk(
            claimed_job,
            claimed_chunk,
            retry_blocks,
            all_blocks,
            TranslationPlan(source_language="en", domain="business", register="neutral"),
        )
        assert len(provider.requests) == 4
        async with restarted.execute(
            "SELECT attempt_no FROM chunk_attempts WHERE chunk_id = ? ORDER BY attempt_no",
            (failed_chunk_id,),
        ) as cursor:
            attempts = await cursor.fetchall()
        assert [int(row[0]) for row in attempts] == list(range(1, 9))
    finally:
        await restarted.close()


async def test_same_payload_race_is_idempotent_and_conflicting_race_is_rejected(
    job_context: dict[str, Any],
) -> None:
    first: JobService = job_context["service"]
    database_path: Path = job_context["database_path"]
    second_connection = await SqliteConnectionFactory(database_path).create()
    second_service = JobService(
        SqliteDocumentRepository(second_connection),
        ApiJobExecutionRepository(second_connection),
        SqliteTranslationCacheRepository(second_connection),
        ModelCostCalculator(),
        persistence=ApiPersistence(second_connection),
        settings=job_context["settings"],
    )
    try:
        same = await asyncio.gather(
            first.create_jobs("doc-1", ["de", "fr"], "request-same-race"),
            second_service.create_jobs("doc-1", ["de", "fr"], "request-same-race"),
        )
        assert {tuple(sorted(job.id for job in jobs)) for jobs in same} == {
            tuple(sorted(job.id for job in same[0]))
        }

        different = await asyncio.gather(
            first.create_jobs("doc-1", ["de"], "request-conflict-race"),
            second_service.create_jobs("doc-1", ["fr"], "request-conflict-race"),
            return_exceptions=True,
        )
        assert sum(isinstance(result, ServiceError) for result in different) == 1
        assert sum(isinstance(result, list) for result in different) == 1
    finally:
        await second_connection.close()


async def test_analysis_changed_before_atomic_enqueue_is_pending_and_rolls_back(
    job_context: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A triage retry during chunk grouping must prevent the eventual job insert."""
    job_repo = job_context["job_repo"]
    original_create = job_repo.create_job_with_chunks

    async def change_analysis_then_create(job, chunks, links):
        other = await SqliteConnectionFactory(job_context["database_path"]).create()
        try:
            async with transaction(other):
                await SqliteDocumentRepository(other).update_document_status(
                    "doc-1", DocumentStatus.ANALYZING
                )
        finally:
            await other.close()
        await original_create(job, chunks, links)

    monkeypatch.setattr(job_repo, "create_job_with_chunks", change_analysis_then_create)
    with pytest.raises(ServiceError) as raised:
        await job_context["service"].create_jobs("doc-1", ["de"], "race")
    assert raised.value.error_code is ErrorCode.ANALYSIS_PENDING
    assert raised.value.status_code == 409
    assert not job_context["connection"].in_transaction
    async with job_context["connection"].execute("SELECT COUNT(*) FROM jobs") as cursor:
        assert (await cursor.fetchone())[0] == 0
    async with job_context["connection"].execute("SELECT COUNT(*) FROM chunks") as cursor:
        assert (await cursor.fetchone())[0] == 0


async def test_analysis_replaced_during_planning_rejects_stale_glossary(
    job_context: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    job_repo = job_context["job_repo"]
    original_create = job_repo.create_job_with_chunks

    async def replace_analysis_then_create(job, chunks, links):
        other = await SqliteConnectionFactory(job_context["database_path"]).create()
        try:
            async with transaction(other):
                async with other.execute(
                    "DELETE FROM document_analyses WHERE document_id = 'doc-1'"
                ):
                    pass
                await SqliteDocumentRepository(other).save_analysis(
                    "doc-1",
                    TranslationPlan(
                        source_language="en", domain="legal", register="formal", terms=["New term"]
                    ),
                )
        finally:
            await other.close()
        await original_create(job, chunks, links)

    monkeypatch.setattr(job_repo, "create_job_with_chunks", replace_analysis_then_create)
    with pytest.raises(ServiceError) as raised:
        await job_context["service"].create_jobs("doc-1", ["de"], "stale-glossary")
    assert raised.value.error_code is ErrorCode.ANALYSIS_PENDING
    async with job_context["connection"].execute("SELECT COUNT(*) FROM jobs") as cursor:
        assert (await cursor.fetchone())[0] == 0
    monkeypatch.setattr(job_repo, "create_job_with_chunks", original_create)
    jobs = await job_context["service"].create_jobs("doc-1", ["de"], "stale-glossary")
    assert jobs[0].glossary == {"New term": "New term"}
