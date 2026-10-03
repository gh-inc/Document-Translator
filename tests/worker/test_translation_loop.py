"""Behavior tests for translation requests, retry accounting, and checkpoints."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from app.adapters.llm.fake_provider import FakeProvider
from app.adapters.llm.pricing import ModelCostCalculator
from app.adapters.persistence.database import SqliteConnectionFactory, transaction
from app.adapters.persistence.repositories import (
    SqliteDocumentRepository,
    SqliteJobExecutionRepository,
    SqliteTranslationCacheRepository,
)
from app.adapters.persistence.worker import WorkerPersistence
from app.config import Settings
from app.core.errors import ErrorCode, ProviderError
from app.core.models import (
    AttemptOutcome,
    Block,
    ChunkAttemptRecord,
    ChunkBlockRecord,
    ChunkRecord,
    ChunkRequest,
    ChunkResult,
    ChunkStatus,
    DocumentStatus,
    JobRecord,
    JobStatus,
    TranslationPlan,
    TriageStatus,
)
from app.core.ports import CostCalculator
from app.core.services.cache_keys import translation_key
from app.worker.translation_loop import TranslationLoop


class TrackingFakeProvider(FakeProvider):
    """FakeProvider that records requests and can inject catalogued failures."""

    def __init__(self, failures: Sequence[ErrorCode] = ()) -> None:
        super().__init__(fail_rate=0, latency_ms=0)
        self.requests = []
        self.failures = list(failures)

    async def translate_chunk(self, request: ChunkRequest) -> ChunkResult:
        self.requests.append(request)
        if self.failures:
            raise ProviderError(
                self.failures.pop(0), tokens_in=9, tokens_out=2, model=request.model
            )
        return await super().translate_chunk(request)


class FixedCostCalculator(CostCalculator):
    def estimate(self, model: str, tokens_in: int, tokens_out: int) -> float:
        return 1.0


class InvalidOutputFakeProvider(FakeProvider):
    async def translate_chunk(self, request: ChunkRequest) -> ChunkResult:
        return ChunkResult(translations={}, tokens_in=4, tokens_out=0, model=request.model)


class BlockingFakeProvider(TrackingFakeProvider):
    def __init__(self, connection: aiosqlite.Connection) -> None:
        super().__init__()
        self.connection = connection
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def translate_chunk(self, request: ChunkRequest) -> ChunkResult:
        assert not self.connection.in_transaction
        self.started.set()
        await self.release.wait()
        return await super().translate_chunk(request)


class DelayedFakeProvider(FakeProvider):
    async def translate_chunk(self, request: ChunkRequest) -> ChunkResult:
        await asyncio.sleep(0.03)
        return await super().translate_chunk(request)


def test_source_neighbors_ignore_empty_structural_blocks_around_a_chunk() -> None:
    blocks = [
        Block(
            id="empty-before",
            seq=0,
            source_text="",
            source_hash="empty-before",
            format_metadata={"future": {"opaque": [1, 2]}},
        ),
        Block(id="before", seq=1, source_text="previous text", source_hash="before"),
        Block(id="chunk", seq=2, source_text="translated text", source_hash="chunk"),
        Block(
            id="empty-after",
            seq=3,
            source_text="",
            source_hash="empty-after",
            format_metadata={"unrecognized": object.__name__},
        ),
        Block(id="after", seq=4, source_text="following text", source_hash="after"),
    ]

    before, after = TranslationLoop._source_neighbors([blocks[2]], blocks)

    assert [block.id for block in before] == ["before"]
    assert [block.id for block in after] == ["after"]


@pytest.fixture
async def worker_fixture(tmp_path: Path) -> AsyncIterator[dict[str, object]]:
    settings = Settings(
        worker_id="worker-1",
        job_lease_seconds=60,
        chunk_lease_seconds=60,
        heartbeat_interval_seconds=10,
    )
    factory = SqliteConnectionFactory(tmp_path / "translation-loop.db")
    connection = await factory.create()
    document_repo = SqliteDocumentRepository(connection)
    job_repo = SqliteJobExecutionRepository(connection, worker_id=settings.worker_id)
    cache_repo = SqliteTranslationCacheRepository(connection)
    blocks = [
        Block(
            id=f"block-{seq}",
            seq=seq,
            source_text=f"Source paragraph {seq}",
            source_hash=f"hash-{seq}",
        )
        for seq in range(4)
    ]
    now = datetime.now(UTC)
    job = JobRecord(
        id="job-1",
        document_id="doc-1",
        batch_id="batch-1",
        target_language="de",
        status=JobStatus.RUNNING,
        total_chunks=2,
        done_chunks=0,
        model="gpt-4o-mini",
        prompt_version="v1",
        glossary={"contract": "Vertrag"},
        tokens_in=0,
        tokens_out=0,
        cost_usd=0.0,
        error_code=None,
        error_detail=None,
        idempotency_key="key-1",
        lease_owner=settings.worker_id,
        lease_expires_at=now + timedelta(minutes=5),
        created_at=now,
        updated_at=now,
    )
    chunk = ChunkRecord(
        id="chunk-1",
        job_id=job.id,
        seq=0,
        status=ChunkStatus.INFLIGHT,
        lease_owner=settings.worker_id,
        lease_expires_at=now + timedelta(minutes=5),
        created_at=now,
    )
    second_chunk = ChunkRecord(
        id="chunk-2",
        job_id=job.id,
        seq=1,
        status=ChunkStatus.INFLIGHT,
        lease_owner=settings.worker_id,
        lease_expires_at=now + timedelta(minutes=5),
        created_at=now,
    )
    async with transaction(connection):
        await document_repo.create_document(
            "doc-1", "source.pdf", "pdf", 100, "/uploads/doc-1/source.pdf", 1
        )
        await document_repo.update_document_status("doc-1", DocumentStatus.EXTRACTED)
        await document_repo.create_blocks("doc-1", blocks)
    await job_repo.create_job_with_chunks(
        job,
        [chunk, second_chunk],
        [
            ChunkBlockRecord(chunk_id=chunk.id, block_id=block.id, seq_in_chunk=block.seq - 1)
            for block in blocks[1:3]
        ]
        + [ChunkBlockRecord(chunk_id=second_chunk.id, block_id=blocks[3].id, seq_in_chunk=0)],
    )
    yield {
        "settings": settings,
        "connection": connection,
        "job_repo": job_repo,
        "cache_repo": cache_repo,
        "persistence": WorkerPersistence(connection),
        "job": job,
        "chunk": chunk,
        "second_chunk": second_chunk,
        "db_path": tmp_path / "translation-loop.db",
        "blocks": blocks,
        "plan": TranslationPlan(
            source_language="en",
            domain="business",
            register="formal",
            terms=["contract"],
            triage_status=TriageStatus.OK,
        ),
    }
    await connection.close()


def _loop(
    fixture: dict[str, object],
    provider: FakeProvider,
    *,
    calculator: CostCalculator | None = None,
    max_attempts: int = 4,
    max_cost: float = 2.0,
) -> TranslationLoop:
    settings = fixture["settings"]
    assert isinstance(settings, Settings)
    configured = settings.model_copy(
        update={"max_chunk_attempts": max_attempts, "max_cost_per_job_usd": max_cost}
    )
    return TranslationLoop(
        configured,
        fixture["job_repo"],  # type: ignore[arg-type]
        fixture["cache_repo"],  # type: ignore[arg-type]
        provider,
        calculator or ModelCostCalculator(),
        persistence=fixture["persistence"],  # type: ignore[arg-type]
    )


async def _attempt_rows(connection: aiosqlite.Connection) -> list[tuple[object, ...]]:
    async with connection.execute(
        "SELECT attempt_no, outcome, cost_usd, latency_ms, error_detail "
        "FROM chunk_attempts ORDER BY attempt_no"
    ) as cursor:
        return await cursor.fetchall()


async def _chunk_state(connection: aiosqlite.Connection) -> tuple[str, int]:
    async with connection.execute(
        "SELECT chunks.status, jobs.done_chunks FROM chunks "
        "JOIN jobs ON jobs.id = chunks.job_id WHERE chunks.id = 'chunk-1'"
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None
    return str(row[0]), int(row[1])


async def test_partial_cache_sends_only_missing_blocks_and_source_neighbors(
    worker_fixture: dict[str, object],
) -> None:
    fixture = worker_fixture
    job = fixture["job"]
    chunk = fixture["chunk"]
    blocks = fixture["blocks"]
    assert isinstance(job, JobRecord) and isinstance(chunk, ChunkRecord)
    assert isinstance(blocks, list)
    cache_repo = fixture["cache_repo"]
    connection = fixture["connection"]
    assert isinstance(connection, aiosqlite.Connection)
    async with transaction(connection):
        await cache_repo.save_block_translation(  # type: ignore[union-attr]
            translation_key(
                job.target_language, job.model, job.prompt_version, job.glossary, fixture["plan"]
            ),
            blocks[1].source_hash,
            "Cached source",
        )

    provider = TrackingFakeProvider()
    loop = _loop(fixture, provider)
    await loop.process_chunk(
        job,
        chunk,
        list(reversed(blocks[1:3])),
        list(reversed(blocks)),
        fixture["plan"],  # type: ignore[arg-type]
    )

    assert len(provider.requests) == 1
    request = provider.requests[0]
    assert [block.id for block in request.blocks] == ["block-2"]
    assert [block.id for block in request.context_before] == ["block-0"]
    assert [block.id for block in request.context_after] == ["block-3"]
    translated = await cache_repo.get_block_translation(  # type: ignore[union-attr]
        translation_key(
            job.target_language, job.model, job.prompt_version, job.glossary, fixture["plan"]
        ),
        blocks[2].source_hash,
    )
    assert translated == "[de] Source paragraph 2"
    refreshed = await fixture["job_repo"].get_job(job.id)  # type: ignore[union-attr]
    assert refreshed is not None
    assert (refreshed.cache_hit_blocks, refreshed.cache_miss_blocks) == (1, 1)
    assert (await _chunk_state(connection)) == ("done", 1)
    attempts = await _attempt_rows(connection)
    assert [(int(row[0]), str(row[1])) for row in attempts] == [(1, "ok")]


async def test_all_cached_chunk_completes_without_provider_call(
    worker_fixture: dict[str, object],
) -> None:
    fixture = worker_fixture
    job = fixture["job"]
    chunk = fixture["chunk"]
    blocks = fixture["blocks"]
    connection = fixture["connection"]
    cache_repo = fixture["cache_repo"]
    assert isinstance(job, JobRecord) and isinstance(chunk, ChunkRecord)
    assert isinstance(connection, aiosqlite.Connection)
    async with transaction(connection):
        for block in blocks[1:3]:
            await cache_repo.save_block_translation(  # type: ignore[union-attr]
                translation_key(
                    job.target_language,
                    job.model,
                    job.prompt_version,
                    job.glossary,
                    fixture["plan"],
                ),
                block.source_hash,
                f"cached {block.id}",
            )

    provider = TrackingFakeProvider()
    await _loop(fixture, provider).process_chunk(
        job,
        chunk,
        blocks[1:3],
        blocks,
        fixture["plan"],  # type: ignore[arg-type]
    )

    assert provider.requests == []
    refreshed = await fixture["job_repo"].get_job(job.id)  # type: ignore[union-attr]
    assert refreshed is not None
    assert (refreshed.cache_hit_blocks, refreshed.cache_miss_blocks) == (2, 0)
    assert await _chunk_state(connection) == ("done", 1)
    assert await _attempt_rows(connection) == []


async def test_repeated_content_without_durable_entry_counts_two_misses(
    worker_fixture: dict[str, object],
) -> None:
    job = worker_fixture["job"]
    chunk = worker_fixture["chunk"]
    blocks = worker_fixture["blocks"]
    assert isinstance(job, JobRecord) and isinstance(chunk, ChunkRecord)
    assert isinstance(blocks, list)
    duplicate = blocks[2].model_copy(
        update={"source_text": blocks[1].source_text, "source_hash": blocks[1].source_hash}
    )
    provider = TrackingFakeProvider()
    await _loop(worker_fixture, provider).process_chunk(
        job,
        chunk,
        [blocks[1], duplicate],
        blocks,
        worker_fixture["plan"],  # type: ignore[arg-type]
    )
    assert len(provider.requests) == 1
    assert [block.id for block in provider.requests[0].blocks] == ["block-1", "block-2"]
    refreshed = await worker_fixture["job_repo"].get_job(job.id)  # type: ignore[union-attr]
    assert refreshed is not None
    assert (refreshed.cache_hit_blocks, refreshed.cache_miss_blocks) == (0, 2)


async def test_retry_attempt_numbers_resume_from_persisted_history(
    worker_fixture: dict[str, object],
) -> None:
    fixture = worker_fixture
    job = fixture["job"]
    chunk = fixture["chunk"]
    blocks = fixture["blocks"]
    connection = fixture["connection"]
    job_repo = fixture["job_repo"]
    assert isinstance(job, JobRecord) and isinstance(chunk, ChunkRecord)
    assert isinstance(connection, aiosqlite.Connection)
    previous_failure = ChunkAttemptRecord(
        id="attempt-before-restart",
        chunk_id=chunk.id,
        attempt_no=1,
        tokens_in=3,
        tokens_out=1,
        cost_usd=0.000001,
        latency_ms=12,
        outcome=AttemptOutcome.RETRYABLE_ERROR,
        error_detail="Translation provider timed out",
        created_at=datetime.now(UTC),
    )
    async with transaction(connection):
        await job_repo.record_chunk_attempt(previous_failure)  # type: ignore[union-attr]

    provider = TrackingFakeProvider([ErrorCode.PROVIDER_TIMEOUT])
    loop = _loop(fixture, provider, max_attempts=3)
    await loop.process_chunk(
        job,
        chunk,
        blocks[1:3],
        blocks,
        fixture["plan"],  # type: ignore[arg-type]
    )

    rows = await _attempt_rows(connection)
    assert [int(row[0]) for row in rows] == [1, 2, 3]
    assert [str(row[1]) for row in rows] == ["retryable_error", "retryable_error", "ok"]
    assert all(int(row[3]) >= 0 for row in rows)
    assert (await _chunk_state(connection)) == ("done", 1)
    assert len(provider.requests) == 2


async def test_fatal_provider_error_is_recorded_with_terminal_checkpoint(
    worker_fixture: dict[str, object],
) -> None:
    fixture = worker_fixture
    job = fixture["job"]
    chunk = fixture["chunk"]
    blocks = fixture["blocks"]
    connection = fixture["connection"]
    assert isinstance(job, JobRecord) and isinstance(chunk, ChunkRecord)
    assert isinstance(connection, aiosqlite.Connection)
    provider = TrackingFakeProvider([ErrorCode.PROVIDER_BAD_REQUEST])

    await _loop(fixture, provider).process_chunk(
        job,
        chunk,
        blocks[1:3],
        blocks,
        fixture["plan"],  # type: ignore[arg-type]
    )

    rows = await _attempt_rows(connection)
    assert len(rows) == 1
    assert (int(rows[0][0]), str(rows[0][1])) == (1, "fatal_error")
    assert rows[0][4] == "Translation provider rejected the request"
    assert (await _chunk_state(connection)) == ("done", 1)


async def test_cost_cap_blocks_call_and_persists_safe_terminal_reason(
    worker_fixture: dict[str, object],
) -> None:
    fixture = worker_fixture
    job = fixture["job"]
    chunk = fixture["chunk"]
    blocks = fixture["blocks"]
    connection = fixture["connection"]
    assert isinstance(job, JobRecord) and isinstance(chunk, ChunkRecord)
    assert isinstance(connection, aiosqlite.Connection)
    provider = TrackingFakeProvider()

    await _loop(fixture, provider, calculator=FixedCostCalculator(), max_cost=0.5).process_chunk(
        job, chunk, blocks[1:3], blocks, fixture["plan"]
    )  # type: ignore[arg-type]

    assert provider.requests == []
    rows = await _attempt_rows(connection)
    assert len(rows) == 1
    assert (int(rows[0][0]), str(rows[0][1]), float(rows[0][2])) == (1, "fatal_error", 0.0)
    assert rows[0][4] == ProviderError(ErrorCode.COST_CAP_EXCEEDED).message
    assert (await _chunk_state(connection)) == ("done", 1)
    refreshed = await fixture["job_repo"].get_job(job.id)  # type: ignore[union-attr]
    assert refreshed is not None
    assert (refreshed.cache_hit_blocks, refreshed.cache_miss_blocks) == (0, 2)


async def test_concurrent_chunks_reserve_soft_cap_before_provider_calls(
    worker_fixture: dict[str, object],
) -> None:
    fixture = worker_fixture
    job = fixture["job"]
    chunk = fixture["chunk"]
    second_chunk = fixture["second_chunk"]
    blocks = fixture["blocks"]
    connection = fixture["connection"]
    assert isinstance(job, JobRecord)
    assert isinstance(chunk, ChunkRecord) and isinstance(second_chunk, ChunkRecord)
    assert isinstance(connection, aiosqlite.Connection)
    provider = BlockingFakeProvider(connection)
    loop = _loop(fixture, provider, calculator=FixedCostCalculator(), max_cost=1.5)
    first_task = asyncio.create_task(
        loop.process_chunk(job, chunk, blocks[1:3], blocks, fixture["plan"])  # type: ignore[arg-type]
    )
    second_task: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        second_task = asyncio.create_task(
            loop.process_chunk(
                job,
                second_chunk,
                blocks[3:4],
                blocks,
                fixture["plan"],  # type: ignore[arg-type]
            )
        )
        await asyncio.wait_for(second_task, timeout=1)
        provider.release.set()
        await first_task
    finally:
        provider.release.set()
        tasks = [first_task] if second_task is None else [first_task, second_task]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    assert len(provider.requests) == 1
    async with connection.execute(
        "SELECT chunks.id, chunks.status, jobs.done_chunks FROM chunks "
        "JOIN jobs ON jobs.id = chunks.job_id ORDER BY chunks.id"
    ) as cursor:
        states = await cursor.fetchall()
    assert [(str(row[0]), str(row[1])) for row in states] == [
        ("chunk-1", "done"),
        ("chunk-2", "done"),
    ]
    assert int(states[0][2]) == 2
    async with connection.execute(
        "SELECT chunk_id, outcome FROM chunk_attempts ORDER BY chunk_id"
    ) as cursor:
        outcomes = await cursor.fetchall()
    assert [(str(row[0]), str(row[1])) for row in outcomes] == [
        ("chunk-1", "ok"),
        ("chunk-2", "fatal_error"),
    ]


async def test_provider_call_has_no_transaction_and_wal_writer_can_progress(
    worker_fixture: dict[str, object],
) -> None:
    fixture = worker_fixture
    job = fixture["job"]
    chunk = fixture["chunk"]
    blocks = fixture["blocks"]
    connection = fixture["connection"]
    assert isinstance(job, JobRecord) and isinstance(chunk, ChunkRecord)
    assert isinstance(blocks, list) and isinstance(connection, aiosqlite.Connection)
    db_path = fixture["db_path"]
    assert isinstance(db_path, Path)
    second_connection = await SqliteConnectionFactory(db_path).create()
    second_document_repo = SqliteDocumentRepository(second_connection)
    provider = BlockingFakeProvider(connection)
    task = asyncio.create_task(
        _loop(fixture, provider).process_chunk(
            job,
            chunk,
            blocks[1:3],
            blocks,
            fixture["plan"],  # type: ignore[arg-type]
        )
    )
    try:
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        assert not connection.in_transaction
        async with transaction(second_connection):
            await asyncio.wait_for(
                second_document_repo.update_document_status(
                    job.document_id, DocumentStatus.EXTRACTED
                ),
                timeout=1,
            )
        provider.release.set()
        await task
    finally:
        provider.release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await second_connection.close()

    assert task.done()
    assert len(provider.requests) == 1


async def test_success_checkpoint_rolls_back_as_one_transaction(
    worker_fixture: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = worker_fixture
    job = fixture["job"]
    chunk = fixture["chunk"]
    blocks = fixture["blocks"]
    connection = fixture["connection"]
    cache_repo = fixture["cache_repo"]
    job_repo = fixture["job_repo"]
    assert isinstance(job, JobRecord) and isinstance(chunk, ChunkRecord)
    assert isinstance(connection, aiosqlite.Connection)

    async def fail_chunk_completion(chunk_id: str) -> None:
        raise RuntimeError("injected checkpoint failure")

    monkeypatch.setattr(job_repo, "complete_chunk", fail_chunk_completion)
    with pytest.raises(RuntimeError, match="injected checkpoint failure"):
        await _loop(fixture, TrackingFakeProvider()).process_chunk(
            job,
            chunk,
            blocks[1:3],
            blocks,
            fixture["plan"],  # type: ignore[arg-type]
        )

    assert (
        await cache_repo.get_block_translation(  # type: ignore[union-attr]
            translation_key(
                job.target_language, job.model, job.prompt_version, job.glossary, fixture["plan"]
            ),
            blocks[1].source_hash,
        )
        is None
    )
    assert (
        await cache_repo.get_block_translation(  # type: ignore[union-attr]
            translation_key(
                job.target_language, job.model, job.prompt_version, job.glossary, fixture["plan"]
            ),
            blocks[2].source_hash,
        )
        is None
    )
    assert await _attempt_rows(connection) == []
    assert await _chunk_state(connection) == ("inflight", 0)


async def test_success_attempt_records_provider_latency(
    worker_fixture: dict[str, object],
) -> None:
    fixture = worker_fixture
    job = fixture["job"]
    chunk = fixture["chunk"]
    blocks = fixture["blocks"]
    connection = fixture["connection"]
    assert isinstance(job, JobRecord) and isinstance(chunk, ChunkRecord)
    assert isinstance(connection, aiosqlite.Connection)

    await _loop(fixture, DelayedFakeProvider(fail_rate=0, latency_ms=0)).process_chunk(
        job,
        chunk,
        blocks[1:3],
        blocks,
        fixture["plan"],  # type: ignore[arg-type]
    )

    rows = await _attempt_rows(connection)
    assert len(rows) == 1
    assert int(rows[0][3]) >= 20


async def test_invalid_provider_block_ids_are_retried_and_terminally_recorded(
    worker_fixture: dict[str, object],
) -> None:
    fixture = worker_fixture
    job = fixture["job"]
    chunk = fixture["chunk"]
    blocks = fixture["blocks"]
    connection = fixture["connection"]
    assert isinstance(job, JobRecord) and isinstance(chunk, ChunkRecord)
    assert isinstance(connection, aiosqlite.Connection)
    provider = InvalidOutputFakeProvider(fail_rate=0, latency_ms=0)

    await _loop(fixture, provider, max_attempts=1).process_chunk(
        job,
        chunk,
        blocks[1:3],
        blocks,
        fixture["plan"],  # type: ignore[arg-type]
    )

    rows = await _attempt_rows(connection)
    assert len(rows) == 1
    assert (int(rows[0][0]), str(rows[0][1])) == (1, "retryable_error")
    assert rows[0][4] == "Translation provider returned an invalid response"
    assert await _chunk_state(connection) == ("done", 1)
