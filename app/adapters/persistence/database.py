"""SQLite connection setup and transaction boundaries."""

import asyncio
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from weakref import WeakKeyDictionary

import aiosqlite
from aiosqlite import Connection

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


@dataclass
class _TransactionState:
    lock: asyncio.Lock
    owner_task: asyncio.Task[object] | None = None


_TRANSACTION_STATES: WeakKeyDictionary[Connection, _TransactionState] = WeakKeyDictionary()


def _state_for(connection: Connection) -> _TransactionState:
    state = _TRANSACTION_STATES.get(connection)
    if state is None:
        state = _TransactionState(lock=asyncio.Lock())
        _TRANSACTION_STATES[connection] = state
    return state


def require_connection_access(connection: Connection) -> None:
    """Reject access from tasks other than a helper-managed transaction owner.

    Manually begun transactions are accepted. Callers that manage one manually
    must serialize both reads and writes themselves.
    """
    state = _TRANSACTION_STATES.get(connection)
    owner = state.owner_task if state is not None else None
    current = asyncio.current_task()
    if owner is not None and current is not owner:
        raise RuntimeError("connection is owned by another asyncio task")


def require_transaction(connection: Connection) -> None:
    """Require an active transaction and enforce managed task ownership."""
    require_connection_access(connection)
    if not connection.in_transaction:
        raise RuntimeError("database writes require an active transaction")


async def _close_cursor(connection: Connection, statement: str) -> str:
    async with connection.execute(statement) as cursor:
        row = await cursor.fetchone()
        return str(row[0]) if row is not None else ""


async def _initialize_wal(connection: Connection) -> str:
    """Retry WAL activation when simultaneous starters race on a legacy file.

    SQLite can return SQLITE_BUSY immediately from ``journal_mode=WAL`` even
    after busy_timeout is set, because switching journal modes needs an
    exclusive lock. Retry for up to 20 seconds; a final SQLite busy wait may
    extend the total startup time.
    """
    deadline = asyncio.get_running_loop().time() + 20.0
    while True:
        try:
            return await _close_cursor(connection, "PRAGMA journal_mode=WAL")
        except aiosqlite.OperationalError as error:
            locked = "locked" in str(error).casefold()
            expired = asyncio.get_running_loop().time() >= deadline
            if not locked or expired:
                raise
            await asyncio.sleep(0.05)


async def _read_schema(path: Path) -> str:
    return await asyncio.to_thread(path.read_text, encoding="utf-8")


async def _migrate_document_analysis_usage(connection: Connection) -> None:
    """Add durable triage usage columns to databases created by older versions.

    Processes inspect the schema without a write lock and acquire BEGIN
    IMMEDIATE only when columns are missing. They recheck after acquiring the
    lock, so a concurrent initializer observes another process's completed
    migration and safely skips the columns it added.
    """
    columns = (
        ("tokens_in", "INTEGER NOT NULL DEFAULT 0"),
        ("tokens_out", "INTEGER NOT NULL DEFAULT 0"),
        ("cost_usd", "REAL NOT NULL DEFAULT 0.0"),
        ("cost_usd_total", "REAL NOT NULL DEFAULT 0.0"),
        ("tokens_in_total", "INTEGER NOT NULL DEFAULT 0"),
        ("tokens_out_total", "INTEGER NOT NULL DEFAULT 0"),
    )

    async def existing_columns() -> set[str]:
        async with connection.execute("PRAGMA table_info(document_analyses)") as cursor:
            return {str(row[1]) for row in await cursor.fetchall()}

    existing = await existing_columns()
    required = {name for name, _ in columns}
    if not existing or required <= existing:
        return

    # The read-only fast path above avoids contending with ordinary writers on
    # every connection open. Recheck after acquiring the lock because another
    # process may have completed the migration since our initial inspection.
    async with transaction(connection):
        existing = await existing_columns()
        if not existing:
            return
        for name, definition in columns:
            if name not in existing:
                async with connection.execute(
                    f"ALTER TABLE document_analyses ADD COLUMN {name} {definition}"
                ):
                    pass


async def _migrate_translation_cache(connection: Connection) -> None:
    """Replace legacy block-ID cache rows and add durable job counters.

    The semantic key now includes the analysis plan, so old rows cannot be
    reused safely. Inspect before taking a write lock and recheck under the
    lock to allow concurrent initializers to finish the same migration.
    """

    async def legacy_cache() -> bool:
        async with connection.execute("PRAGMA table_info(block_translations)") as cursor:
            columns = {str(row[1]) for row in await cursor.fetchall()}
        return "block_id" in columns

    async def missing_counters() -> list[str]:
        async with connection.execute("PRAGMA table_info(jobs)") as cursor:
            columns = {str(row[1]) for row in await cursor.fetchall()}
        if not columns:
            return []
        return [name for name in ("cache_hit_blocks", "cache_miss_blocks") if name not in columns]

    if not await legacy_cache() and not await missing_counters():
        return

    async with transaction(connection):
        if await legacy_cache():
            async with connection.execute("DROP TABLE block_translations"):
                pass
            async with connection.execute(
                "CREATE TABLE block_translations ("
                "translation_key TEXT NOT NULL, source_hash TEXT NOT NULL, "
                "translated_text TEXT NOT NULL, "
                "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                "PRIMARY KEY (translation_key, source_hash))"
            ):
                pass
            async with connection.execute(
                "CREATE INDEX idx_block_translations_lookup "
                "ON block_translations(translation_key, source_hash)"
            ):
                pass
        for name in await missing_counters():
            async with connection.execute(
                f"ALTER TABLE jobs ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0"
            ):
                pass


async def _finish_cleanup(awaitable: Awaitable[object]) -> None:
    """Finish connection cleanup even if the caller is being cancelled."""
    task = asyncio.ensure_future(awaitable)
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    await task


async def _wait_for_task(task: asyncio.Future[Connection]) -> Connection:
    """Wait for connection opening to finish without cancelling its worker."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


async def _commit_through_cancellation(connection: Connection) -> None:
    """Finish a submitted COMMIT before honoring cancellation.

    aiosqlite queues SQLite operations independently of the awaiting task. Once
    COMMIT is submitted, rolling it back after a caller cancellation can be too
    late: SQLite may already have committed. Treat that point as the transaction
    boundary, consume cancellations delivered while waiting for COMMIT, and
    report the committed outcome as success.
    """
    commit_task = asyncio.create_task(connection.commit())
    current = asyncio.current_task()
    while not commit_task.done():
        try:
            await asyncio.shield(commit_task)
        except asyncio.CancelledError:
            if current is not None:
                current.uncancel()
            continue
    await commit_task


class SqliteConnectionFactory:
    """Creates configured connections and optionally loads the shared DDL."""

    def __init__(self, db_path: Path, *, init_schema: bool = True) -> None:
        self._db_path = db_path
        self._init_schema = init_schema

    async def create(self) -> Connection:
        """Open a connection, initialize its pragmas and optionally load DDL."""
        if str(self._db_path) != ":memory:":
            await asyncio.to_thread(self._db_path.parent.mkdir, parents=True, exist_ok=True)

        opening = asyncio.ensure_future(aiosqlite.connect(str(self._db_path), isolation_level=None))
        try:
            connection = await asyncio.shield(opening)
        except BaseException:
            with suppress(BaseException):
                connection = await _wait_for_task(opening)
                await _finish_cleanup(connection.close())
            raise
        try:
            connection.row_factory = aiosqlite.Row

            # busy_timeout must be installed before requesting WAL mode.
            await _close_cursor(connection, "PRAGMA busy_timeout=20000")
            journal_mode = await _initialize_wal(connection)
            await _close_cursor(connection, "PRAGMA synchronous=NORMAL")
            await _close_cursor(connection, "PRAGMA foreign_keys=ON")

            is_memory_db = str(self._db_path) == ":memory:"
            expected_mode = "memory" if is_memory_db else "wal"
            if journal_mode.casefold() != expected_mode:
                raise RuntimeError(
                    f"SQLite journal mode initialization failed: expected {expected_mode}"
                )

            if self._init_schema:
                # A legacy cache may lack its old named index. Running the new
                # DDL first would try to create the source_hash index on the
                # still-legacy table and fail before migration can replace it.
                await _migrate_translation_cache(connection)
                schema = await _read_schema(SCHEMA_PATH)
                async with connection.executescript(schema):
                    pass
                await _migrate_document_analysis_usage(connection)
            return connection
        except BaseException:
            with suppress(BaseException):
                await _finish_cleanup(connection.close())
            raise


@asynccontextmanager
async def transaction(connection: Connection) -> AsyncIterator[Connection]:
    """Run a serialized BEGIN IMMEDIATE transaction on ``connection``.

    Cancellation before commit rolls the transaction back. Once COMMIT has
    been submitted, cancellation is deferred until its outcome is known; a
    successful commit returns success, while a failed commit is rolled back.
    """
    state = _state_for(connection)
    current = asyncio.current_task()
    if current is None:
        raise RuntimeError("database transactions require an asyncio task")
    if state.owner_task is current:
        raise RuntimeError("nested transaction contexts are not supported")

    await state.lock.acquire()
    try:
        if connection.in_transaction:
            raise RuntimeError(
                "cannot enter transaction context while a caller-owned transaction is active"
            )

        state.owner_task = current
        try:
            async with connection.execute("BEGIN IMMEDIATE"):
                pass
            yield connection
        except BaseException:
            with suppress(BaseException):
                await _finish_cleanup(connection.rollback())
            raise
        else:
            try:
                await _commit_through_cancellation(connection)
            except BaseException:
                with suppress(BaseException):
                    await _finish_cleanup(connection.rollback())
                raise
    finally:
        state.owner_task = None
        state.lock.release()
