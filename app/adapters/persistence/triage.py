"""Internal persistence support for replacing degraded document analyses."""

from aiosqlite import Connection

from app.adapters.persistence.database import require_connection_access, require_transaction


class TriagePersistence:
    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    async def has_jobs(self, document_id: str) -> bool:
        require_connection_access(self._connection)
        async with self._connection.execute(
            "SELECT 1 FROM jobs WHERE document_id = ? LIMIT 1", (document_id,)
        ) as cursor:
            return await cursor.fetchone() is not None

    async def discard_degraded_analysis(self, document_id: str) -> None:
        require_transaction(self._connection)
        async with self._connection.execute(
            "DELETE FROM document_analyses WHERE document_id = ? AND triage_status = 'degraded'",
            (document_id,),
        ):
            pass
