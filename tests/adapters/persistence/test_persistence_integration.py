"""Independent persistence checks across WAL connections and process boundaries."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from app.adapters.persistence.database import SqliteConnectionFactory, transaction
from app.adapters.persistence.repositories import (
    SqliteDocumentRepository,
    SqliteJobExecutionRepository,
    SqliteTranslationCacheRepository,
)
from app.core.models import (
    AttemptOutcome,
    Block,
    ChunkAttemptRecord,
    ChunkBlockRecord,
    ChunkRecord,
    ChunkStatus,
    JobRecord,
    JobStatus,
)


@pytest.fixture
async def connections(tmp_path: Path) -> AsyncIterator[tuple[aiosqlite.Connection, ...]]:
    factory = SqliteConnectionFactory(tmp_path / "integration.db")
    first = await factory.create()
    try:
        second = await SqliteConnectionFactory(
            tmp_path / "integration.db", init_schema=False
        ).create()
        try:
            yield first, second
        finally:
            await second.close()
    finally:
        await first.close()


async def _seed_document(connection: aiosqlite.Connection) -> Block:
    block = Block(
        id="block",
        seq=0,
        source_text="Hello",
        source_hash="source-hash",
        format_metadata={"vendor": {"bbox": "invalid", "unknown": [None, True, "українська"]}},
    )
    repository = SqliteDocumentRepository(connection)
    async with transaction(connection):
        await repository.create_document("document", "source.pdf", "pdf", 5, "/uploads/document")
        await repository.create_blocks("document", [block])
    return block


def _aggregate(
    job_id: str = "job", key: str = "request"
) -> tuple[JobRecord, list[ChunkRecord], list[ChunkBlockRecord]]:
    now = datetime.now(UTC)
    job = JobRecord(
        id=job_id,
        document_id="document",
        batch_id="batch",
        target_language="de",
        status=JobStatus.QUEUED,
        total_chunks=1,
        done_chunks=0,
        model="test-model",
        prompt_version="v1",
        tokens_in=0,
        tokens_out=0,
        cost_usd=0,
        error_code=None,
        error_detail=None,
        idempotency_key=key,
        lease_owner=None,
        lease_expires_at=None,
        created_at=now,
        updated_at=now,
    )
    chunk = ChunkRecord(
        id=job_id + "-chunk",
        job_id=job_id,
        seq=0,
        status=ChunkStatus.PENDING,
        lease_owner=None,
        lease_expires_at=None,
        created_at=now,
    )
    return job, [chunk], [ChunkBlockRecord(chunk_id=chunk.id, block_id="block", seq_in_chunk=0)]


def _attempt(attempt_id: str, cost: float) -> ChunkAttemptRecord:
    return ChunkAttemptRecord(
        id=attempt_id,
        chunk_id="job-chunk",
        attempt_no=1 if attempt_id == "first" else 2,
        tokens_in=10,
        tokens_out=5,
        cost_usd=cost,
        latency_ms=10,
        outcome=AttemptOutcome.OK,
        error_detail=None,
        created_at=datetime.now(UTC),
    )


async def test_concurrent_claims_grant_one_job_and_chunk_lease(
    connections: tuple[aiosqlite.Connection, ...],
) -> None:
    await _seed_document(connections[0])
    await SqliteJobExecutionRepository(connections[0]).create_job_with_chunks(*_aggregate())
    lease = datetime.now(UTC) + timedelta(minutes=1)

    async def claim_job(connection: aiosqlite.Connection, worker: str) -> JobRecord | None:
        async with transaction(connection):
            return await SqliteJobExecutionRepository(connection).claim_job(worker, lease)

    claimed = await asyncio.gather(
        claim_job(connections[0], "worker-a"), claim_job(connections[1], "worker-b")
    )
    assert len([job for job in claimed if job is not None]) == 1

    async def claim_chunk(connection: aiosqlite.Connection, worker: str) -> ChunkRecord | None:
        async with transaction(connection):
            return await SqliteJobExecutionRepository(connection).claim_chunk("job", worker, lease)

    chunks = await asyncio.gather(
        claim_chunk(connections[0], "worker-a"), claim_chunk(connections[1], "worker-b")
    )
    assert len([chunk for chunk in chunks if chunk is not None]) == 1


async def test_concurrent_idempotent_enqueue_keeps_one_aggregate(
    connections: tuple[aiosqlite.Connection, ...],
) -> None:
    await _seed_document(connections[0])
    await asyncio.gather(
        SqliteJobExecutionRepository(connections[0]).create_job_with_chunks(
            *_aggregate("job-a", "same-request")
        ),
        SqliteJobExecutionRepository(connections[1]).create_job_with_chunks(
            *_aggregate("job-b", "same-request")
        ),
    )
    repository = SqliteJobExecutionRepository(connections[1])
    jobs = await repository.get_jobs_by_batch("batch")
    assert len(jobs) == 1
    assert len(await repository.get_pending_chunks(jobs[0].id)) == 1
    async with connections[1].execute("SELECT COUNT(*) FROM chunk_blocks") as cursor:
        assert tuple(await cursor.fetchone()) == (1,)


async def test_concurrent_cache_writes_preserve_result_and_record_all_spend(
    connections: tuple[aiosqlite.Connection, ...],
) -> None:
    await _seed_document(connections[0])
    await SqliteJobExecutionRepository(connections[0]).create_job_with_chunks(*_aggregate())

    async def checkpoint(
        connection: aiosqlite.Connection, result: str, attempt: ChunkAttemptRecord
    ) -> None:
        async with transaction(connection):
            await SqliteTranslationCacheRepository(connection).save_block_translation(
                "translation-key", "block", result
            )
            await SqliteJobExecutionRepository(connection).record_chunk_attempt(attempt)

    await asyncio.gather(
        checkpoint(connections[0], "Hallo", _attempt("first", 0.1)),
        checkpoint(connections[1], "Guten Tag", _attempt("second", 0.2)),
    )
    result = await SqliteTranslationCacheRepository(connections[0]).get_block_translation(
        "translation-key", "block"
    )
    assert result in {"Hallo", "Guten Tag"}
    job = await SqliteJobExecutionRepository(connections[0]).get_job("job")
    assert job is not None
    assert (job.tokens_in, job.tokens_out) == (20, 10)
    assert job.cost_usd == pytest.approx(0.3)
    async with connections[0].execute("SELECT COUNT(*) FROM block_translations") as cursor:
        assert tuple(await cursor.fetchone()) == (1,)


async def test_translation_and_attempt_checkpoint_roll_back_together(
    connections: tuple[aiosqlite.Connection, ...],
) -> None:
    await _seed_document(connections[0])
    await SqliteJobExecutionRepository(connections[0]).create_job_with_chunks(*_aggregate())
    with pytest.raises(RuntimeError, match="injected checkpoint failure"):
        async with transaction(connections[0]):
            await SqliteTranslationCacheRepository(connections[0]).save_block_translation(
                "translation-key", "block", "Hallo"
            )
            await SqliteJobExecutionRepository(connections[0]).record_chunk_attempt(
                _attempt("first", 0.1)
            )
            raise RuntimeError("injected checkpoint failure")

    assert (
        await SqliteTranslationCacheRepository(connections[1]).get_block_translation(
            "translation-key", "block"
        )
        is None
    )
    job = await SqliteJobExecutionRepository(connections[1]).get_job("job")
    assert job is not None
    assert (job.tokens_in, job.tokens_out, job.cost_usd) == (0, 0, 0)
    async with connections[1].execute("SELECT COUNT(*) FROM chunk_attempts") as cursor:
        assert tuple(await cursor.fetchone()) == (0,)


async def test_committed_metadata_cache_and_expired_leases_survive_reopen(
    connections: tuple[aiosqlite.Connection, ...], tmp_path: Path
) -> None:
    first = connections[0]
    original = await _seed_document(first)
    await SqliteJobExecutionRepository(first).create_job_with_chunks(*_aggregate())
    expired = datetime.now(UTC) - timedelta(minutes=1)
    async with transaction(first):
        repository = SqliteJobExecutionRepository(first)
        assert await repository.claim_job("old-worker", expired) is not None
        assert await repository.claim_chunk("job", "old-worker", expired) is not None
        await SqliteTranslationCacheRepository(first).save_block_translation(
            "translation-key", "block", "Hallo"
        )
    await first.close()

    reopened = await SqliteConnectionFactory(
        tmp_path / "integration.db", init_schema=False
    ).create()
    try:
        assert await SqliteDocumentRepository(reopened).get_blocks("document") == [original]
        async with transaction(reopened):
            repository = SqliteJobExecutionRepository(reopened)
            released = await repository.release_expired_chunks(datetime.now(UTC))
            assert len(released) == 1
            assert released[0].status is ChunkStatus.PENDING
            recovered = await repository.claim_job(
                "new-worker", datetime.now(UTC) + timedelta(minutes=1)
            )
            assert recovered is not None
            assert recovered.status is JobStatus.RUNNING
            assert recovered.lease_owner == "new-worker"
            assert (
                await repository.claim_chunk(
                    "job", "new-worker", datetime.now(UTC) + timedelta(minutes=1)
                )
                is not None
            )
        assert (
            await SqliteTranslationCacheRepository(reopened).get_block_translation(
                "translation-key", "block"
            )
            == "Hallo"
        )
    finally:
        await reopened.close()


async def test_cancelled_enqueue_leaves_no_partial_rows_and_connection_is_reusable(
    connections: tuple[aiosqlite.Connection, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    first = connections[0]
    await _seed_document(first)
    reached_join_write = asyncio.Event()
    release_write = asyncio.Event()
    execute_many = first.executemany

    @asynccontextmanager
    async def delayed_join_write(
        statement: str, parameters: Iterable[Iterable[object]]
    ) -> AsyncIterator[aiosqlite.Cursor]:
        if statement.startswith("INSERT INTO chunk_blocks"):
            reached_join_write.set()
            await release_write.wait()
        async with execute_many(statement, parameters) as cursor:
            yield cursor

    monkeypatch.setattr(first, "executemany", delayed_join_write)
    enqueue = asyncio.create_task(
        SqliteJobExecutionRepository(first).create_job_with_chunks(*_aggregate())
    )
    try:
        await asyncio.wait_for(reached_join_write.wait(), timeout=5)
        enqueue.cancel()
        with pytest.raises(asyncio.CancelledError):
            await enqueue
    finally:
        if not enqueue.done():
            enqueue.cancel()
            await asyncio.gather(enqueue, return_exceptions=True)
        monkeypatch.setattr(first, "executemany", execute_many)

    assert not first.in_transaction
    assert await SqliteJobExecutionRepository(connections[1]).get_job("job") is None
    for query in ("SELECT COUNT(*) FROM chunks", "SELECT COUNT(*) FROM chunk_blocks"):
        async with connections[1].execute(query) as cursor:
            assert tuple(await cursor.fetchone()) == (0,)
    await SqliteJobExecutionRepository(first).create_job_with_chunks(*_aggregate())
    assert await SqliteJobExecutionRepository(connections[1]).get_job("job") is not None


async def test_other_task_cannot_read_uncommitted_rows_on_shared_connection(
    connections: tuple[aiosqlite.Connection, ...],
) -> None:
    first, second = connections
    repository = SqliteDocumentRepository(first)
    with pytest.raises(RuntimeError, match="abort uncommitted write"):
        async with transaction(first):
            await repository.create_document(
                "uncommitted", "source.pdf", "pdf", 5, "/uploads/uncommitted"
            )
            assert await repository.get_document("uncommitted") is not None
            with pytest.raises(RuntimeError, match="owned by another"):
                await asyncio.create_task(repository.get_document("uncommitted"))
            assert await SqliteDocumentRepository(second).get_document("uncommitted") is None
            raise RuntimeError("abort uncommitted write")
    assert await repository.get_document("uncommitted") is None


async def test_cancellation_during_commit_reports_success_for_complete_aggregate(
    connections: tuple[aiosqlite.Connection, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    first = connections[0]
    await _seed_document(first)
    commit_started = asyncio.Event()
    complete_commit = asyncio.Event()
    original_commit = first.commit

    async def delayed_commit() -> None:
        commit_started.set()
        await complete_commit.wait()
        await original_commit()

    monkeypatch.setattr(first, "commit", delayed_commit)
    enqueue = asyncio.create_task(
        SqliteJobExecutionRepository(first).create_job_with_chunks(*_aggregate())
    )
    try:
        await asyncio.wait_for(commit_started.wait(), timeout=5)
        enqueue.cancel()
        complete_commit.set()
        assert await enqueue is None
    finally:
        complete_commit.set()
        await asyncio.gather(enqueue, return_exceptions=True)
        monkeypatch.setattr(first, "commit", original_commit)

    assert not enqueue.cancelled()
    assert enqueue.cancelling() == 0
    assert not first.in_transaction
    repository = SqliteJobExecutionRepository(connections[1])
    assert await repository.get_job("job") is not None
    assert len(await repository.get_pending_chunks("job")) == 1
    async with connections[1].execute("SELECT COUNT(*) FROM chunk_blocks") as cursor:
        assert tuple(await cursor.fetchone()) == (1,)
