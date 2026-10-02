"""Worker reads and checkpoints share one serialized connection boundary."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.adapters.persistence.database import SqliteConnectionFactory
from app.adapters.persistence.repositories import SqliteJobExecutionRepository
from app.adapters.persistence.worker import LeaseLostError, WorkerPersistence
from tests.adapters.persistence.test_persistence_integration import _aggregate, _seed_document


async def test_worker_reads_wait_until_checkpoint_commits(tmp_path: Path) -> None:
    connection = await SqliteConnectionFactory(tmp_path / "worker.db").create()
    persistence = WorkerPersistence(connection)
    repository = SqliteJobExecutionRepository(connection)
    await _seed_document(connection)
    await repository.create_job_with_chunks(*_aggregate())
    entered = asyncio.Event()
    release = asyncio.Event()

    async def writer() -> None:
        async with persistence.write():
            await repository.claim_job("worker", datetime.now(UTC) + timedelta(minutes=1))
            entered.set()
            await release.wait()

    async def reader() -> str | None:
        async with persistence.read():
            job = await repository.get_job("job")
            assert job is not None
            return job.lease_owner

    task = asyncio.create_task(writer())
    try:
        await entered.wait()
        reading = asyncio.create_task(reader())
        await asyncio.sleep(0)
        assert not reading.done()
        release.set()
        await task
        assert await reading == "worker"
        async with persistence.read():
            await persistence.ensure_owned("job", "worker")
            with pytest.raises(LeaseLostError):
                await persistence.ensure_owned("job", "stale")
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await connection.close()


async def test_worker_loads_membership_and_rejects_expired_leases(tmp_path: Path) -> None:
    connection = await SqliteConnectionFactory(tmp_path / "worker.db").create()
    persistence = WorkerPersistence(connection)
    repository = SqliteJobExecutionRepository(connection)
    try:
        block = await _seed_document(connection)
        await repository.create_job_with_chunks(*_aggregate())
        async with persistence.write():
            await repository.claim_job("worker", datetime.now(UTC) - timedelta(seconds=1))
        async with persistence.read():
            assert await persistence.get_chunk_blocks("job-chunk") == [block]
            assert await persistence.next_attempt_no("job-chunk") == 1
            assert await persistence.unfinished_chunks("job") == 1
            with pytest.raises(LeaseLostError):
                await persistence.ensure_owned("job", "worker")
    finally:
        await connection.close()
