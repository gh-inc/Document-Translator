"""Internal worker coordination on a shared SQLite connection.

Repository ports remain transport independent. This implementation helper
serializes worker reads with its service transactions and loads execution-only
details that are absent from the public repository contracts.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from aiosqlite import Connection

from app.adapters.persistence.database import require_connection_access, transaction
from app.adapters.persistence.repositories import SqliteDocumentRepository
from app.core.models import Block


class LeaseLostError(Exception):
    """Internal control flow when execution ownership has expired or moved."""


class WorkerPersistence:
    """Coordinate every read/write using one worker-owned connection."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def read(self) -> AsyncIterator[None]:
        async with self._lock:
            require_connection_access(self._connection)
            yield

    @asynccontextmanager
    async def write(self) -> AsyncIterator[None]:
        async with self._lock, transaction(self._connection):
            yield

    async def get_chunk_blocks(self, chunk_id: str) -> list[Block]:
        require_connection_access(self._connection)
        async with self._connection.execute(
            "SELECT chunks.job_id, jobs.document_id FROM chunks "
            "JOIN jobs ON jobs.id = chunks.job_id WHERE chunks.id = ?",
            (chunk_id,),
        ) as cursor:
            chunk = await cursor.fetchone()
        if chunk is None:
            raise ValueError("chunk does not exist")
        blocks = await SqliteDocumentRepository(self._connection).get_blocks(chunk["document_id"])
        by_id = {block.id: block for block in blocks}
        async with self._connection.execute(
            "SELECT block_id FROM chunk_blocks WHERE chunk_id = ? ORDER BY seq_in_chunk",
            (chunk_id,),
        ) as cursor:
            links = await cursor.fetchall()
        return [by_id[link["block_id"]] for link in links]

    async def next_attempt_no(self, chunk_id: str) -> int:
        require_connection_access(self._connection)
        async with self._connection.execute(
            "SELECT COALESCE(MAX(attempt_no), 0) + 1 FROM chunk_attempts WHERE chunk_id = ?",
            (chunk_id,),
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        return int(row[0])

    async def ensure_owned(self, job_id: str, worker_id: str, chunk_id: str | None = None) -> None:
        require_connection_access(self._connection)
        now = datetime.now(UTC).isoformat(timespec="microseconds")
        async with self._connection.execute(
            "SELECT 1 FROM jobs WHERE id = ? AND lease_owner = ? "
            "AND lease_expires_at > ? AND status IN ('running', 'assembling')",
            (job_id, worker_id, now),
        ) as cursor:
            owned = await cursor.fetchone()
        if owned is None:
            raise LeaseLostError("job execution lease lost")
        if chunk_id is not None:
            async with self._connection.execute(
                "SELECT 1 FROM chunks WHERE id = ? AND job_id = ? AND lease_owner = ? "
                "AND lease_expires_at > ? AND status = 'inflight'",
                (chunk_id, job_id, worker_id, now),
            ) as cursor:
                owned = await cursor.fetchone()
            if owned is None:
                raise LeaseLostError("chunk execution lease lost")

    async def unfinished_chunks(self, job_id: str) -> int:
        require_connection_access(self._connection)
        async with self._connection.execute(
            "SELECT COUNT(*) FROM chunks WHERE job_id = ? AND status != 'done'",
            (job_id,),
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        return int(row[0])
