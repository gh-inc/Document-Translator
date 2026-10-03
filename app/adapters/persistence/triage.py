"""Internal persistence for conditional triage claims and short connection scopes."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from aiosqlite import Connection

from app.adapters.persistence.database import (
    SqliteConnectionFactory,
    _finish_cleanup,
    require_connection_access,
    require_transaction,
    transaction,
)
from app.adapters.persistence.repositories import SqliteDocumentRepository
from app.core.models import (
    Block,
    DocumentAnalysisRecord,
    DocumentRecord,
    DocumentStatus,
    TranslationPlan,
)


class TriagePersistence:
    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    async def has_jobs(self, document_id: str) -> bool:
        require_connection_access(self._connection)
        async with self._connection.execute(
            "SELECT 1 FROM jobs WHERE document_id = ? LIMIT 1", (document_id,)
        ) as cursor:
            return await cursor.fetchone() is not None

    async def claim_analysis(self, document_id: str) -> bool:
        """Conditionally claim eligible analysis while its advisory lock is held.

        ANALYZING is eligible for recovery only because the caller already
        owns the cross-process lock; a live owner cannot make this update.
        """
        require_transaction(self._connection)
        async with self._connection.execute(
            """UPDATE documents SET status = 'analyzing', error_code = NULL
               WHERE id = ? AND status IN ('uploaded', 'analyzing', 'extracted')
                 AND NOT EXISTS (
                     SELECT 1 FROM document_analyses
                     WHERE document_id = documents.id AND triage_status = 'ok'
                 )
                 AND NOT EXISTS (SELECT 1 FROM jobs WHERE document_id = documents.id)""",
            (document_id,),
        ) as cursor:
            return cursor.rowcount == 1

    async def discard_degraded_analysis(self, document_id: str) -> None:
        require_transaction(self._connection)
        async with self._connection.execute(
            "DELETE FROM document_analyses WHERE document_id = ? AND triage_status = 'degraded'",
            (document_id,),
        ):
            pass


class ScopedTriageRepository:
    """One claim's persistence; connections never survive an agent call."""

    def __init__(self, database_path: Path) -> None:
        self._factory = SqliteConnectionFactory(database_path, init_schema=False)
        self._connection: Connection | None = None

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Connection]:
        if self._connection is not None:
            raise RuntimeError("nested triage transactions are not supported")
        connection = await self._factory.create()
        self._connection = connection
        try:
            async with transaction(connection):
                yield connection
        finally:
            self._connection = None
            await _finish_cleanup(connection.close())

    async def _read[T](self, operation: Callable[[SqliteDocumentRepository], Awaitable[T]]) -> T:
        if self._connection is not None:
            return await operation(SqliteDocumentRepository(self._connection))
        connection = await self._factory.create()
        try:
            return await operation(SqliteDocumentRepository(connection))
        finally:
            await _finish_cleanup(connection.close())

    def _active_connection(self) -> Connection:
        if self._connection is None:
            raise RuntimeError("triage writes require a service transaction")
        return self._connection

    async def get_document(self, document_id: str) -> DocumentRecord | None:
        return await self._read(lambda repo: repo.get_document(document_id))

    async def get_blocks(self, document_id: str) -> list[Block]:
        return await self._read(lambda repo: repo.get_blocks(document_id))

    async def get_analysis(self, document_id: str) -> DocumentAnalysisRecord | None:
        return await self._read(lambda repo: repo.get_analysis(document_id))

    async def save_analysis(
        self,
        document_id: str,
        plan: TranslationPlan,
        *,
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost_usd: float = 0.0,
        cost_usd_total: float | None = None,
        tokens_in_total: int | None = None,
        tokens_out_total: int | None = None,
    ) -> DocumentAnalysisRecord:
        return await SqliteDocumentRepository(self._active_connection()).save_analysis(
            document_id,
            plan,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
            cost_usd_total=cost_usd_total,
            tokens_in_total=tokens_in_total,
            tokens_out_total=tokens_out_total,
        )

    async def update_document_status(
        self, document_id: str, status: DocumentStatus, error_code: str | None = None
    ) -> None:
        await SqliteDocumentRepository(self._active_connection()).update_document_status(
            document_id, status, error_code
        )

    async def claim_analysis(self, document_id: str) -> bool:
        return await TriagePersistence(self._active_connection()).claim_analysis(document_id)

    async def has_jobs(self, document_id: str) -> bool:
        return await TriagePersistence(self._active_connection()).has_jobs(document_id)

    async def discard_degraded_analysis(self, document_id: str) -> None:
        await TriagePersistence(self._active_connection()).discard_degraded_analysis(document_id)
