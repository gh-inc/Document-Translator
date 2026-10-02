"""Contract tests for the SQLite repository adapters."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta, timezone
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
    DocumentStatus,
    JobError,
    JobRecord,
    JobStatus,
    TranslationPlan,
    TriageStatus,
)


@pytest.fixture
async def repositories(
    tmp_path: Path,
) -> AsyncIterator[
    tuple[
        SqliteDocumentRepository,
        SqliteJobExecutionRepository,
        SqliteTranslationCacheRepository,
        aiosqlite.Connection,
    ]
]:
    factory = SqliteConnectionFactory(tmp_path / "repositories.db")
    connection = await factory.create()
    yield (
        SqliteDocumentRepository(connection),
        SqliteJobExecutionRepository(connection),
        SqliteTranslationCacheRepository(connection),
        connection,
    )
    await connection.close()


def _job(
    *,
    job_id: str = "job-1",
    document_id: str = "doc-1",
    idempotency_key: str = "key-1",
    total_chunks: int = 1,
    status: JobStatus = JobStatus.QUEUED,
    lease_expires_at: datetime | None = None,
) -> JobRecord:
    now = datetime.now(UTC)
    return JobRecord(
        id=job_id,
        document_id=document_id,
        batch_id="batch-1",
        target_language="de",
        status=status,
        total_chunks=total_chunks,
        done_chunks=0,
        model="test-model",
        prompt_version="v1",
        glossary={"Hello": "Hallo"},
        tokens_in=0,
        tokens_out=0,
        cost_usd=0.0,
        error_code=None,
        error_detail=None,
        idempotency_key=idempotency_key,
        lease_owner=None,
        lease_expires_at=lease_expires_at,
        created_at=now,
        updated_at=now,
    )


def _chunk(
    *,
    chunk_id: str = "chunk-1",
    job_id: str = "job-1",
    seq: int = 0,
    status: ChunkStatus = ChunkStatus.PENDING,
    lease_expires_at: datetime | None = None,
) -> ChunkRecord:
    return ChunkRecord(
        id=chunk_id,
        job_id=job_id,
        seq=seq,
        status=status,
        lease_owner=None,
        lease_expires_at=lease_expires_at,
        created_at=datetime.now(UTC),
    )


def _block(block_id: str = "block-1", seq: int = 0) -> Block:
    return Block(
        id=block_id,
        seq=seq,
        source_text=f"Source {seq}",
        source_hash=f"hash-{seq}",
        format_metadata={
            "nested": {"unrecognized": [1, None, {"ключ": "значение"}]},
            "bbox": [1.25, 2, 3, 4],
        },
    )


async def _seed_document(
    document_repository: SqliteDocumentRepository,
    blocks: list[Block] | None = None,
) -> None:
    async with transaction(document_repository._connection):
        await document_repository.create_document(
            "doc-1", "source.pdf", "pdf", 123, "/uploads/doc-1/source.pdf", 2
        )
        if blocks is not None:
            await document_repository.create_blocks("doc-1", blocks)


async def _seed_job(
    document_repository: SqliteDocumentRepository,
    job_repository: SqliteJobExecutionRepository,
    *,
    job: JobRecord | None = None,
    chunks: list[ChunkRecord] | None = None,
    links: list[ChunkBlockRecord] | None = None,
) -> JobRecord:
    await _seed_document(
        document_repository,
        [_block("block-1", 0), _block("block-2", 1)],
    )
    selected_job = job or _job(total_chunks=1)
    selected_chunks = [_chunk()] if chunks is None else chunks
    selected_links = (
        [ChunkBlockRecord(chunk_id="chunk-1", block_id="block-1", seq_in_chunk=0)]
        if links is None
        else links
    )
    await job_repository.create_job_with_chunks(selected_job, selected_chunks, selected_links)
    return selected_job


async def test_document_lifecycle_roundtrips_opaque_metadata_and_immutable_analysis(
    repositories: tuple[
        SqliteDocumentRepository,
        SqliteJobExecutionRepository,
        SqliteTranslationCacheRepository,
        aiosqlite.Connection,
    ],
) -> None:
    document_repository, _, _, connection = repositories
    first = _block("block-1", 2)
    second = _block("block-2", 1)
    async with transaction(connection):
        created = await document_repository.create_document(
            "doc-1", "source.pdf", "pdf", 123, "/uploads/doc-1/source.pdf", 2
        )
        await document_repository.create_blocks("doc-1", [first, second])
        await document_repository.update_document_status("doc-1", DocumentStatus.EXTRACTED)
        first_plan = TranslationPlan(
            source_language="en",
            domain="legal",
            register="formal",
            terms=["agreement"],
            warnings=["scan has footnotes"],
            triage_status=TriageStatus.OK,
        )
        saved_analysis = await document_repository.save_analysis("doc-1", first_plan)
        replacement = TranslationPlan(
            source_language="fr",
            domain="medical",
            register="informal",
            terms=["nouveau"],
            warnings=[],
            triage_status=TriageStatus.DEGRADED,
        )
        reread_analysis = await document_repository.save_analysis("doc-1", replacement)

    assert created.status is DocumentStatus.UPLOADED
    assert created.created_at.tzinfo == UTC
    assert await document_repository.get_document("doc-1") == created.model_copy(
        update={"status": DocumentStatus.EXTRACTED}
    )
    found_blocks = await document_repository.get_blocks("doc-1")
    assert [block.seq for block in found_blocks] == [1, 2]
    assert found_blocks == [second, first]
    assert reread_analysis == saved_analysis
    assert (await document_repository.get_analysis("doc-1")) == saved_analysis
    assert reread_analysis.created_at.tzinfo == UTC
    # Repository instances borrow the injected connection; reads leave it open.
    async with connection.execute("SELECT 1") as cursor:
        assert (await cursor.fetchone())[0] == 1


async def test_ordinary_mutations_require_caller_transaction(
    repositories: tuple[
        SqliteDocumentRepository,
        SqliteJobExecutionRepository,
        SqliteTranslationCacheRepository,
        aiosqlite.Connection,
    ],
) -> None:
    document_repository, _, _, _ = repositories
    with pytest.raises(RuntimeError, match="transaction"):
        await document_repository.create_document(
            "doc-1", "source.pdf", "pdf", 1, "/uploads/source.pdf"
        )


async def test_reads_reject_another_tasks_uncommitted_transaction(
    repositories: tuple[
        SqliteDocumentRepository,
        SqliteJobExecutionRepository,
        SqliteTranslationCacheRepository,
        aiosqlite.Connection,
    ],
) -> None:
    document_repository, _, _, connection = repositories
    write_started = asyncio.Event()
    allow_commit = asyncio.Event()

    async def writer() -> None:
        async with transaction(connection):
            await document_repository.create_document(
                "uncommitted-doc", "source.pdf", "pdf", 1, "/uploads/source.pdf"
            )
            write_started.set()
            await allow_commit.wait()

    async def reader() -> None:
        await write_started.wait()
        with pytest.raises(RuntimeError, match="another asyncio task"):
            await document_repository.get_document("uncommitted-doc")
        allow_commit.set()

    await asyncio.gather(writer(), reader())
    assert await document_repository.get_document("uncommitted-doc") is not None


async def test_aggregate_is_atomic_idempotent_and_validates_membership(
    repositories: tuple[
        SqliteDocumentRepository,
        SqliteJobExecutionRepository,
        SqliteTranslationCacheRepository,
        aiosqlite.Connection,
    ],
) -> None:
    document_repository, job_repository, _, connection = repositories
    original = await _seed_job(document_repository, job_repository)
    duplicate = _job(job_id="ignored-job", idempotency_key=original.idempotency_key)
    await job_repository.create_job_with_chunks(
        duplicate,
        [_chunk(chunk_id="ignored-chunk", job_id="ignored-job")],
        [],
    )
    assert await job_repository.get_job("ignored-job") is None
    assert await job_repository.get_job(original.id) == original
    assert len(await job_repository.get_pending_chunks(original.id)) == 1

    invalid = _job(job_id="invalid-job", idempotency_key="invalid-key")
    with pytest.raises(ValueError, match="job document"):
        await job_repository.create_job_with_chunks(
            invalid,
            [_chunk(chunk_id="invalid-chunk", job_id="invalid-job")],
            [ChunkBlockRecord(chunk_id="invalid-chunk", block_id="missing-block", seq_in_chunk=0)],
        )
    assert await job_repository.get_job("invalid-job") is None

    # Duplicate sequence numbers in links fail after the job and chunks were inserted.
    rollback_job = _job(job_id="rollback-job", idempotency_key="rollback-key")
    with pytest.raises(aiosqlite.IntegrityError):
        await job_repository.create_job_with_chunks(
            rollback_job,
            [_chunk(chunk_id="rollback-chunk", job_id="rollback-job")],
            [
                ChunkBlockRecord(chunk_id="rollback-chunk", block_id="block-1", seq_in_chunk=0),
                ChunkBlockRecord(chunk_id="rollback-chunk", block_id="block-2", seq_in_chunk=0),
            ],
        )
    assert await job_repository.get_job("rollback-job") is None
    assert await job_repository.get_pending_chunks("rollback-job") == []
    async with connection.execute("SELECT COUNT(*) FROM chunk_blocks") as cursor:
        assert (await cursor.fetchone())[0] == 1


async def test_aggregate_rejects_active_caller_transaction_without_rolling_it_back(
    repositories: tuple[
        SqliteDocumentRepository,
        SqliteJobExecutionRepository,
        SqliteTranslationCacheRepository,
        aiosqlite.Connection,
    ],
) -> None:
    document_repository, job_repository, _, connection = repositories
    async with transaction(connection):
        await document_repository.create_document(
            "doc-1", "source.pdf", "pdf", 1, "/uploads/source.pdf"
        )
        with pytest.raises(RuntimeError, match="nested transaction"):
            await job_repository.create_job_with_chunks(_job(), [_chunk()], [])
        assert connection.in_transaction


async def test_job_and_chunk_lifecycle_progress_heartbeats_and_terminal_status(
    repositories: tuple[
        SqliteDocumentRepository,
        SqliteJobExecutionRepository,
        SqliteTranslationCacheRepository,
        aiosqlite.Connection,
    ],
) -> None:
    document_repository, job_repository, _, connection = repositories
    job = await _seed_job(
        document_repository,
        job_repository,
        job=_job(total_chunks=2),
        chunks=[_chunk(chunk_id="chunk-1", seq=0), _chunk(chunk_id="chunk-2", seq=1)],
        links=[ChunkBlockRecord(chunk_id="chunk-1", block_id="block-1", seq_in_chunk=0)],
    )
    assert await job_repository.get_jobs_by_batch(job.batch_id) == [job]
    lease = datetime.now(UTC) + timedelta(minutes=5)
    async with transaction(connection):
        claimed_job = await job_repository.claim_job("worker-1", lease)
    assert claimed_job is not None and claimed_job.status is JobStatus.RUNNING
    assert claimed_job.lease_owner == "worker-1"
    assert claimed_job.lease_expires_at is not None
    assert claimed_job.lease_expires_at.tzinfo == UTC

    async with transaction(connection):
        await job_repository.heartbeat_job(job.id, lease + timedelta(minutes=2))
        await job_repository.heartbeat_job(job.id, lease)
        await job_repository.update_job_progress(job.id, 99)
        await job_repository.update_job_progress(job.id, 1)
        claimed_chunk = await job_repository.claim_chunk(
            job.id,
            "worker-1",
            lease.astimezone(timezone(timedelta(hours=3))),
        )
        assert claimed_chunk is not None
        assert claimed_chunk.status is ChunkStatus.INFLIGHT
        assert claimed_chunk.lease_expires_at == lease
        assert claimed_chunk.lease_expires_at.tzinfo == UTC
        await job_repository.heartbeat_chunk("chunk-1", lease + timedelta(minutes=2))
        await job_repository.heartbeat_chunk("chunk-1", lease)
        async with connection.execute(
            "SELECT lease_expires_at FROM chunks WHERE id = ?", ("chunk-1",)
        ) as cursor:
            chunk_lease = datetime.fromisoformat((await cursor.fetchone())[0])
        assert chunk_lease == lease + timedelta(minutes=2)
        await job_repository.complete_chunk("chunk-1")
    updated = await job_repository.get_job(job.id)
    assert updated is not None
    assert updated.done_chunks == 2
    assert updated.lease_expires_at == lease + timedelta(minutes=2)
    assert (await job_repository.get_pending_chunks(job.id))[0].id == "chunk-2"
    async with connection.execute(
        "SELECT status, lease_owner, lease_expires_at FROM chunks WHERE id = ?", ("chunk-1",)
    ) as cursor:
        completed_chunk = await cursor.fetchone()
    assert tuple(completed_chunk) == ("done", None, None)

    async with transaction(connection):
        await job_repository.complete_job(
            job.id,
            JobStatus.COMPLETED_WITH_ERRORS,
            JobError(error_code="provider_failed", message="One chunk failed", retryable=False),
        )
    terminal = await job_repository.get_job(job.id)
    assert terminal is not None
    assert terminal.status is JobStatus.COMPLETED_WITH_ERRORS
    assert terminal.error_code == "provider_failed"
    assert terminal.error_detail == "One chunk failed"
    assert terminal.lease_owner is None and terminal.lease_expires_at is None
    async with transaction(connection):
        await job_repository.complete_job(job.id, JobStatus.DONE)
    assert await job_repository.get_job(job.id) == terminal
    with pytest.raises(ValueError, match="terminal"):
        async with transaction(connection):
            await job_repository.complete_job(job.id, JobStatus.RUNNING)


async def test_job_claim_reclaims_expired_running_and_assembling_without_regression(
    repositories: tuple[
        SqliteDocumentRepository,
        SqliteJobExecutionRepository,
        SqliteTranslationCacheRepository,
        aiosqlite.Connection,
    ],
) -> None:
    document_repository, job_repository, _, connection = repositories
    await _seed_document(document_repository)
    now = datetime.now(UTC)
    running = _job(
        job_id="running-job",
        idempotency_key="running-key",
        total_chunks=0,
        status=JobStatus.RUNNING,
        lease_expires_at=now - timedelta(seconds=1),
    )
    assembling = _job(
        job_id="assembling-job",
        idempotency_key="assembling-key",
        total_chunks=0,
        status=JobStatus.ASSEMBLING,
        lease_expires_at=now - timedelta(seconds=1),
    )
    await job_repository.create_job_with_chunks(running, [], [])
    await job_repository.create_job_with_chunks(assembling, [], [])
    async with transaction(connection):
        first = await job_repository.claim_job("worker-2", now + timedelta(minutes=1))
    assert first is not None and first.id == "running-job"
    assert first.status is JobStatus.RUNNING
    async with transaction(connection):
        second = await job_repository.claim_job("worker-2", now + timedelta(minutes=1))
    assert second is not None and second.id == "assembling-job"
    assert second.status is JobStatus.ASSEMBLING


async def test_running_job_enters_assembling_keeps_lease_then_is_reclaimable(
    repositories: tuple[
        SqliteDocumentRepository,
        SqliteJobExecutionRepository,
        SqliteTranslationCacheRepository,
        aiosqlite.Connection,
    ],
) -> None:
    document_repository, job_repository, _, connection = repositories
    await _seed_document(document_repository)
    expired_lease = datetime.now(UTC) - timedelta(seconds=1)
    running = _job(
        job_id="assembly-job",
        idempotency_key="assembly-key",
        total_chunks=0,
        status=JobStatus.RUNNING,
        lease_expires_at=expired_lease,
    ).model_copy(update={"lease_owner": "worker-1"})
    await job_repository.create_job_with_chunks(running, [], [])
    async with transaction(connection):
        await job_repository.complete_job(running.id, JobStatus.ASSEMBLING)
    assembling = await job_repository.get_job(running.id)
    assert assembling is not None
    assert assembling.status is JobStatus.ASSEMBLING
    assert assembling.lease_owner == "worker-1"
    assert assembling.lease_expires_at == expired_lease
    async with transaction(connection):
        reclaimed = await job_repository.claim_job(
            "worker-2", datetime.now(UTC) + timedelta(minutes=1)
        )
    assert reclaimed is not None
    assert reclaimed.status is JobStatus.ASSEMBLING
    assert reclaimed.lease_owner == "worker-2"


async def test_worker_scoped_mutations_are_fenced_by_job_and_chunk_leases(
    repositories: tuple[
        SqliteDocumentRepository,
        SqliteJobExecutionRepository,
        SqliteTranslationCacheRepository,
        aiosqlite.Connection,
    ],
) -> None:
    document_repository, admin_repository, _, connection = repositories
    stale_worker = SqliteJobExecutionRepository(connection, worker_id="worker-old")
    active_worker = SqliteJobExecutionRepository(connection, worker_id="worker-new")
    await _seed_document(document_repository)
    expired_job_lease = datetime.now(UTC) - timedelta(seconds=1)
    active_chunk_lease = datetime.now(UTC) + timedelta(minutes=5)
    job = _job(
        job_id="fenced-job",
        idempotency_key="fenced-key",
        status=JobStatus.RUNNING,
        lease_expires_at=expired_job_lease,
    ).model_copy(update={"lease_owner": "worker-old"})
    chunk = _chunk(
        chunk_id="fenced-chunk",
        job_id=job.id,
        status=ChunkStatus.INFLIGHT,
        lease_expires_at=active_chunk_lease,
    ).model_copy(update={"lease_owner": "worker-old"})
    await admin_repository.create_job_with_chunks(job, [chunk], [])

    renewed_job_lease = datetime.now(UTC) + timedelta(minutes=10)
    with pytest.raises(ValueError, match="bound worker_id"):
        async with transaction(connection):
            await active_worker.claim_job("caller-supplied-other-id", renewed_job_lease)
    async with transaction(connection):
        reclaimed = await active_worker.claim_job("worker-new", renewed_job_lease)
    assert reclaimed is not None
    assert reclaimed.lease_owner == "worker-new"

    stale_extension = datetime.now(UTC) + timedelta(hours=1)
    async with transaction(connection):
        await stale_worker.heartbeat_job(job.id, stale_extension)
        await stale_worker.update_job_progress(job.id, 1)
        await stale_worker.complete_job(job.id, JobStatus.ASSEMBLING)
        await stale_worker.complete_job(job.id, JobStatus.DONE)
        await stale_worker.heartbeat_chunk(chunk.id, stale_extension)
        await stale_worker.complete_chunk(chunk.id)
    after_stale_mutations = await admin_repository.get_job(job.id)
    assert after_stale_mutations is not None
    assert after_stale_mutations.status is JobStatus.RUNNING
    assert after_stale_mutations.done_chunks == 0
    assert after_stale_mutations.lease_owner == "worker-new"
    assert after_stale_mutations.lease_expires_at == renewed_job_lease
    async with connection.execute(
        "SELECT status, lease_owner, lease_expires_at FROM chunks WHERE id = ?", (chunk.id,)
    ) as cursor:
        stale_chunk_state = await cursor.fetchone()
    assert tuple(stale_chunk_state) == (
        ChunkStatus.INFLIGHT.value,
        "worker-old",
        active_chunk_lease.isoformat(timespec="microseconds"),
    )

    expired_at = (datetime.now(UTC) - timedelta(seconds=1)).isoformat(timespec="microseconds")
    expire_chunk = connection.execute(
        "UPDATE chunks SET lease_expires_at = ? WHERE id = ?", (expired_at, chunk.id)
    )
    async with transaction(connection), expire_chunk:
        pass
    async with transaction(connection):
        released = await admin_repository.release_expired_chunks(datetime.now(UTC))
    assert [record.id for record in released] == [chunk.id]
    with pytest.raises(ValueError, match="bound worker_id"):
        async with transaction(connection):
            await active_worker.claim_chunk(
                job.id,
                "caller-supplied-other-id",
                datetime.now(UTC) + timedelta(minutes=5),
            )
    async with transaction(connection):
        claimed_chunk = await active_worker.claim_chunk(
            job.id,
            "worker-new",
            datetime.now(UTC) + timedelta(minutes=5),
        )
    assert claimed_chunk is not None
    assert claimed_chunk.lease_owner == "worker-new"

    async with transaction(connection):
        await active_worker.heartbeat_job(job.id, renewed_job_lease + timedelta(minutes=1))
        await active_worker.update_job_progress(job.id, 1)
        await active_worker.heartbeat_chunk(chunk.id, renewed_job_lease + timedelta(minutes=1))
        await active_worker.complete_chunk(chunk.id)
        await active_worker.complete_job(job.id, JobStatus.ASSEMBLING)
        await active_worker.complete_job(job.id, JobStatus.DONE)
    final_job = await admin_repository.get_job(job.id)
    assert final_job is not None
    assert final_job.status is JobStatus.DONE
    assert final_job.done_chunks == 1
    assert final_job.lease_owner is None and final_job.lease_expires_at is None
    async with connection.execute(
        "SELECT status, lease_owner, lease_expires_at FROM chunks WHERE id = ?", (chunk.id,)
    ) as cursor:
        final_chunk_state = await cursor.fetchone()
    assert tuple(final_chunk_state) == (ChunkStatus.DONE.value, None, None)


async def test_release_expired_chunks_includes_missing_lease_and_keeps_active_lease(
    repositories: tuple[
        SqliteDocumentRepository,
        SqliteJobExecutionRepository,
        SqliteTranslationCacheRepository,
        aiosqlite.Connection,
    ],
) -> None:
    document_repository, job_repository, _, connection = repositories
    await _seed_document(document_repository)
    now = datetime.now(UTC)
    chunks = [
        _chunk(
            chunk_id="expired",
            seq=0,
            status=ChunkStatus.INFLIGHT,
            lease_expires_at=now - timedelta(microseconds=1),
        ),
        _chunk(
            chunk_id="active",
            seq=1,
            status=ChunkStatus.INFLIGHT,
            lease_expires_at=now + timedelta(minutes=1),
        ),
        _chunk(chunk_id="stranded", seq=2, status=ChunkStatus.INFLIGHT),
    ]
    await job_repository.create_job_with_chunks(_job(total_chunks=3), chunks, [])
    async with transaction(connection):
        released = await job_repository.release_expired_chunks(now)
    assert [chunk.id for chunk in released] == ["expired", "stranded"]
    assert all(chunk.status is ChunkStatus.PENDING for chunk in released)
    assert {chunk.id for chunk in await job_repository.get_pending_chunks("job-1")} == {
        "expired",
        "stranded",
    }


async def test_record_attempts_bill_all_outcomes_to_the_job_in_same_transaction(
    repositories: tuple[
        SqliteDocumentRepository,
        SqliteJobExecutionRepository,
        SqliteTranslationCacheRepository,
        aiosqlite.Connection,
    ],
) -> None:
    document_repository, job_repository, _, connection = repositories
    seeded_job = _job().model_copy(update={"tokens_in": 100, "tokens_out": 200, "cost_usd": 2.0})
    job = await _seed_job(document_repository, job_repository, job=seeded_job)
    attempts = [
        ChunkAttemptRecord(
            id="attempt-1",
            chunk_id="chunk-1",
            attempt_no=1,
            tokens_in=10,
            tokens_out=5,
            cost_usd=0.01,
            latency_ms=100,
            outcome=AttemptOutcome.RETRYABLE_ERROR,
            error_detail="timeout",
            created_at=datetime.now(UTC),
        ),
        ChunkAttemptRecord(
            id="attempt-2",
            chunk_id="chunk-1",
            attempt_no=2,
            tokens_in=20,
            tokens_out=8,
            cost_usd=0.02,
            latency_ms=200,
            outcome=AttemptOutcome.OK,
            error_detail=None,
            created_at=datetime.now(UTC),
        ),
        ChunkAttemptRecord(
            id="attempt-3",
            chunk_id="chunk-1",
            attempt_no=3,
            tokens_in=2,
            tokens_out=1,
            cost_usd=0.005,
            latency_ms=50,
            outcome=AttemptOutcome.FATAL_ERROR,
            error_detail="context length exceeded",
            created_at=datetime.now(UTC),
        ),
    ]
    async with transaction(connection):
        for attempt in attempts:
            await job_repository.record_chunk_attempt(attempt)
    updated = await job_repository.get_job(job.id)
    assert updated is not None
    assert (updated.tokens_in, updated.tokens_out) == (32, 14)
    assert updated.cost_usd == pytest.approx(0.035)
    async with connection.execute(
        "SELECT COUNT(*) FROM chunk_attempts WHERE chunk_id = ?", ("chunk-1",)
    ) as cursor:
        assert (await cursor.fetchone())[0] == 3


async def test_concurrent_aggregate_creates_serialize_on_injected_connection(
    repositories: tuple[
        SqliteDocumentRepository,
        SqliteJobExecutionRepository,
        SqliteTranslationCacheRepository,
        aiosqlite.Connection,
    ],
) -> None:
    document_repository, job_repository, _, _ = repositories
    await _seed_document(document_repository)
    first = _job(job_id="parallel-1", idempotency_key="parallel-key-1", total_chunks=0)
    second = _job(job_id="parallel-2", idempotency_key="parallel-key-2", total_chunks=0)
    await asyncio.gather(
        job_repository.create_job_with_chunks(first, [], []),
        job_repository.create_job_with_chunks(second, [], []),
    )
    assert await job_repository.get_job(first.id) == first
    assert await job_repository.get_job(second.id) == second


async def test_cache_write_is_idempotent_first_translation_wins(
    repositories: tuple[
        SqliteDocumentRepository,
        SqliteJobExecutionRepository,
        SqliteTranslationCacheRepository,
        aiosqlite.Connection,
    ],
) -> None:
    document_repository, _, cache_repository, connection = repositories
    await _seed_document(document_repository, [_block()])
    async with transaction(connection):
        await cache_repository.save_block_translation("key-1", "block-1", "Hallo")
    async with transaction(connection):
        await cache_repository.save_block_translation("key-1", "block-1", "Überschrieben")
    assert await cache_repository.get_block_translation("key-1", "block-1") == "Hallo"
    assert await cache_repository.get_block_translation("missing", "block-1") is None
