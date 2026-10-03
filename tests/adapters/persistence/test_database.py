import asyncio
import sqlite3
from pathlib import Path
from threading import Event as ThreadEvent

import aiosqlite
import pytest

from app.adapters.persistence import database
from app.adapters.persistence.database import (
    SqliteConnectionFactory,
    require_connection_access,
    require_transaction,
    transaction,
)


async def _scalar(connection: aiosqlite.Connection, statement: str) -> object:
    async with connection.execute(statement) as cursor:
        row = await cursor.fetchone()
    assert row is not None
    return row[0]


@pytest.mark.asyncio
async def test_factory_initializes_pragmas_and_schema_on_file_connection(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "nested" / "app.db"
    factory = SqliteConnectionFactory(db_path)

    connection = await factory.create()
    try:
        assert connection.isolation_level is None
        assert connection.row_factory is aiosqlite.Row
        assert await _scalar(connection, "PRAGMA journal_mode") == "wal"
        assert await _scalar(connection, "PRAGMA synchronous") == 1
        assert await _scalar(connection, "PRAGMA foreign_keys") == 1
        assert await _scalar(connection, "PRAGMA busy_timeout") == 20000
        assert (
            await _scalar(
                connection,
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='documents'",
            )
            == 1
        )
    finally:
        await connection.close()

    # Connection-local PRAGMAs are reapplied on every new connection.
    reopened = await SqliteConnectionFactory(db_path, init_schema=False).create()
    try:
        assert await _scalar(reopened, "PRAGMA journal_mode") == "wal"
        assert await _scalar(reopened, "PRAGMA synchronous") == 1
        assert await _scalar(reopened, "PRAGMA foreign_keys") == 1
        assert await _scalar(reopened, "PRAGMA busy_timeout") == 20000
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_factory_migrates_legacy_analysis_rows_and_serializes_startup(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as legacy:
        legacy.executescript(
            """
            CREATE TABLE documents (
                id TEXT PRIMARY KEY, filename TEXT NOT NULL, format TEXT NOT NULL,
                size_bytes INTEGER NOT NULL, page_count INTEGER, storage_path TEXT NOT NULL,
                status TEXT NOT NULL, error_code TEXT,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE document_analyses (
                document_id TEXT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
                source_language TEXT NOT NULL, domain TEXT NOT NULL, register TEXT NOT NULL,
                terms TEXT NOT NULL DEFAULT '[]', warnings TEXT NOT NULL DEFAULT '[]',
                triage_status TEXT NOT NULL,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO documents (id, filename, format, size_bytes, storage_path, status)
            VALUES ('doc-legacy', 'old.pdf', 'pdf', 12, '/uploads/old.pdf', 'extracted');
            INSERT INTO document_analyses (
                document_id, source_language, domain, register, triage_status
            ) VALUES ('doc-legacy', 'en', 'general', 'neutral', 'ok');
            """
        )

    connections = await asyncio.gather(
        SqliteConnectionFactory(db_path).create(),
        SqliteConnectionFactory(db_path).create(),
    )
    try:
        expected = {
            "tokens_in": 0,
            "tokens_out": 0,
            "cost_usd": 0.0,
            "cost_usd_total": 0.0,
            "tokens_in_total": 0,
            "tokens_out_total": 0,
        }
        for connection in connections:
            async with connection.execute(
                "SELECT tokens_in, tokens_out, cost_usd, cost_usd_total, "
                "tokens_in_total, tokens_out_total FROM document_analyses "
                "WHERE document_id = 'doc-legacy'"
            ) as cursor:
                row = await cursor.fetchone()
            assert row is not None
            assert dict(row) == expected
            async with connection.execute("PRAGMA table_info(document_analyses)") as cursor:
                names = {str(info[1]) for info in await cursor.fetchall()}
            assert names >= expected.keys()
    finally:
        await asyncio.gather(*(connection.close() for connection in connections))


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_index", [False, True])
async def test_factory_migrates_legacy_cache_and_job_counters(
    tmp_path: Path, legacy_index: bool
) -> None:
    db_path = tmp_path / "legacy-cache.db"
    with sqlite3.connect(db_path) as legacy:
        legacy.executescript(
            """
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY, document_id TEXT NOT NULL, batch_id TEXT NOT NULL,
                target_language TEXT NOT NULL, status TEXT NOT NULL,
                total_chunks INTEGER NOT NULL DEFAULT 0,
                done_chunks INTEGER NOT NULL DEFAULT 0, model TEXT NOT NULL,
                prompt_version TEXT NOT NULL, glossary TEXT NOT NULL DEFAULT '{}',
                tokens_in INTEGER NOT NULL DEFAULT 0,
                tokens_out INTEGER NOT NULL DEFAULT 0,
                cost_usd REAL NOT NULL DEFAULT 0.0, error_code TEXT,
                error_detail TEXT, idempotency_key TEXT NOT NULL UNIQUE,
                lease_owner TEXT, lease_expires_at DATETIME,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO jobs (id, document_id, batch_id, target_language,
                status, model, prompt_version, idempotency_key)
            VALUES ('job-old', 'doc-old', 'batch-old', 'de', 'done', 'model', 'v1', 'idem');
            CREATE TABLE block_translations (
                translation_key TEXT NOT NULL, block_id TEXT NOT NULL,
                translated_text TEXT NOT NULL,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (translation_key, block_id)
            );
            INSERT INTO block_translations (translation_key, block_id, translated_text)
            VALUES ('legacy-key', 'old-block', 'Hallo');
            """
        )
        if legacy_index:
            legacy.execute(
                "CREATE INDEX idx_block_translations_lookup "
                "ON block_translations(translation_key, block_id)"
            )
    legacy.close()

    connections = await asyncio.gather(
        SqliteConnectionFactory(db_path).create(),
        SqliteConnectionFactory(db_path).create(),
    )
    try:
        for connection in connections:
            async with connection.execute("PRAGMA table_info(block_translations)") as cursor:
                cache_columns = {str(row[1]) for row in await cursor.fetchall()}
            assert "source_hash" in cache_columns
            assert "block_id" not in cache_columns
            assert await _scalar(connection, "SELECT COUNT(*) FROM block_translations") == 0
            async with connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='block_translations' AND name NOT LIKE 'sqlite_autoindex_%'"
            ) as cursor:
                indexes = {str(row[0]) for row in await cursor.fetchall()}
            assert indexes == {"idx_block_translations_lookup"}
            async with connection.execute("PRAGMA table_info(jobs)") as cursor:
                job_columns = {str(row[1]): row[4] for row in await cursor.fetchall()}
            assert job_columns["cache_hit_blocks"] == "0"
            assert job_columns["cache_miss_blocks"] == "0"
            async with connection.execute(
                "SELECT cache_hit_blocks, cache_miss_blocks FROM jobs WHERE id = 'job-old'"
            ) as cursor:
                row = await cursor.fetchone()
            assert tuple(row) == (0, 0)
    finally:
        await asyncio.gather(*(connection.close() for connection in connections))


@pytest.mark.asyncio
async def test_migrated_factory_open_does_not_wait_for_unrelated_writer(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "already-migrated.db"
    initial = await SqliteConnectionFactory(db_path).create()
    await initial.close()

    writer = await SqliteConnectionFactory(db_path, init_schema=False).create()
    try:
        async with writer.execute("BEGIN IMMEDIATE"):
            pass
        reopened = await asyncio.wait_for(SqliteConnectionFactory(db_path).create(), 1.0)
        await reopened.close()
    finally:
        await writer.rollback()
        await writer.close()


@pytest.mark.asyncio
async def test_factory_supports_memory_mode_and_optional_schema() -> None:
    connection = await SqliteConnectionFactory(Path(":memory:"), init_schema=False).create()
    try:
        assert await _scalar(connection, "PRAGMA journal_mode") == "memory"
        assert (
            await _scalar(
                connection,
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='documents'",
            )
            == 0
        )
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_factory_reopen_preserves_file_database(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    first = await SqliteConnectionFactory(db_path, init_schema=False).create()
    try:
        async with first.execute("CREATE TABLE sample (value TEXT NOT NULL)"):
            pass
        async with first.execute("INSERT INTO sample VALUES ('persisted')"):
            pass
    finally:
        await first.close()

    second = await SqliteConnectionFactory(db_path, init_schema=False).create()
    try:
        assert await _scalar(second, "SELECT value FROM sample") == "persisted"
    finally:
        await second.close()


@pytest.mark.asyncio
async def test_factory_closes_connection_after_initialization_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    opened: aiosqlite.Connection | None = None
    original_connect = aiosqlite.connect

    async def capture_connection(*args: object, **kwargs: object) -> aiosqlite.Connection:
        nonlocal opened
        opened = await original_connect(*args, **kwargs)
        return opened

    async def fail_read(_path: Path) -> str:
        raise OSError("schema read failed")

    monkeypatch.setattr(database.aiosqlite, "connect", capture_connection)
    monkeypatch.setattr(database, "_read_schema", fail_read)

    with pytest.raises(OSError, match="schema read failed"):
        await SqliteConnectionFactory(tmp_path / "app.db").create()

    assert opened is not None
    with pytest.raises(ValueError):
        await opened.execute("SELECT 1")


@pytest.mark.asyncio
async def test_factory_closes_connection_after_cancellation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    opened: aiosqlite.Connection | None = None
    started = asyncio.Event()
    original_connect = aiosqlite.connect

    async def capture_connection(*args: object, **kwargs: object) -> aiosqlite.Connection:
        nonlocal opened
        opened = await original_connect(*args, **kwargs)
        return opened

    async def wait_for_cancel(_path: Path) -> str:
        started.set()
        await asyncio.Event().wait()
        return ""

    monkeypatch.setattr(database.aiosqlite, "connect", capture_connection)
    monkeypatch.setattr(database, "_read_schema", wait_for_cancel)
    task = asyncio.create_task(SqliteConnectionFactory(tmp_path / "app.db").create())

    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert opened is not None
    with pytest.raises(ValueError):
        await opened.execute("SELECT 1")


@pytest.mark.asyncio
async def test_factory_finishes_open_and_closes_on_open_cancellation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    opened: aiosqlite.Connection | None = None
    opening_started = asyncio.Event()
    continue_opening = asyncio.Event()
    original_connect = aiosqlite.connect

    async def delayed_connect(*args: object, **kwargs: object) -> aiosqlite.Connection:
        nonlocal opened
        opening_started.set()
        await continue_opening.wait()
        opened = await original_connect(*args, **kwargs)
        return opened

    monkeypatch.setattr(database.aiosqlite, "connect", delayed_connect)
    task = asyncio.create_task(SqliteConnectionFactory(tmp_path / "app.db").create())

    await opening_started.wait()
    task.cancel()
    continue_opening.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert opened is not None
    with pytest.raises(ValueError):
        await opened.execute("SELECT 1")


@pytest.mark.asyncio
async def test_transaction_commits_and_requires_active_transaction(
    tmp_path: Path,
) -> None:
    connection = await SqliteConnectionFactory(tmp_path / "app.db", init_schema=False).create()
    try:
        async with connection.execute("CREATE TABLE sample (value TEXT NOT NULL)"):
            pass
        with pytest.raises(RuntimeError, match="active transaction"):
            require_transaction(connection)

        async with transaction(connection):
            require_transaction(connection)
            async with connection.execute("INSERT INTO sample VALUES ('committed')"):
                pass

        assert await _scalar(connection, "SELECT value FROM sample") == "committed"
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_transaction_rolls_back_on_body_and_commit_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    connection = await SqliteConnectionFactory(tmp_path / "app.db", init_schema=False).create()
    try:
        async with connection.execute("CREATE TABLE sample (value TEXT NOT NULL)"):
            pass
        with pytest.raises(ValueError, match="body failed"):
            async with transaction(connection):
                async with connection.execute("INSERT INTO sample VALUES ('body')"):
                    pass
                raise ValueError("body failed")
        assert await _scalar(connection, "SELECT COUNT(*) FROM sample") == 0

        async def fail_commit() -> None:
            raise sqlite3.OperationalError("commit failed")

        monkeypatch.setattr(connection, "commit", fail_commit)
        with pytest.raises(sqlite3.OperationalError, match="commit failed"):
            await _insert_in_transaction(connection, "commit")
        assert not connection.in_transaction
        assert await _scalar(connection, "SELECT COUNT(*) FROM sample") == 0
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_transaction_commit_completes_through_cancellation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    connection = await SqliteConnectionFactory(tmp_path / "app.db", init_schema=False).create()
    commit_started = asyncio.Event()
    allow_commit = asyncio.Event()
    original_commit = connection.commit
    try:
        async with connection.execute("CREATE TABLE sample (value TEXT NOT NULL)"):
            pass

        async def delayed_commit() -> None:
            commit_started.set()
            await allow_commit.wait()
            await original_commit()

        monkeypatch.setattr(connection, "commit", delayed_commit)

        async def write_rows() -> int:
            async with (
                transaction(connection),
                connection.executemany("INSERT INTO sample VALUES (?)", [("first",), ("second",)]),
            ):
                pass
            current = asyncio.current_task()
            assert current is not None
            return current.cancelling()

        task = asyncio.create_task(write_rows())
        await commit_started.wait()
        task.cancel()
        allow_commit.set()

        # Cancellation delivered while waiting for COMMIT is consumed once the
        # successful commit outcome is known.
        assert await task == 0
        assert not connection.in_transaction
        async with connection.execute("SELECT value FROM sample ORDER BY value") as cursor:
            rows = await cursor.fetchall()
        assert [row["value"] for row in rows] == ["first", "second"]

        await _insert_in_transaction(connection, "connection-usable")
        assert await _scalar(connection, "SELECT COUNT(*) FROM sample") == 3
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_transaction_rejects_nesting_and_preserves_manual_transaction(
    tmp_path: Path,
) -> None:
    connection = await SqliteConnectionFactory(tmp_path / "app.db", init_schema=False).create()
    try:
        async with connection.execute("CREATE TABLE sample (value TEXT NOT NULL)"):
            pass

        async with transaction(connection):
            with pytest.raises(RuntimeError, match="nested"):
                async with transaction(connection):
                    pass
            assert connection.in_transaction

        async with connection.execute("BEGIN"):
            pass
        with pytest.raises(RuntimeError, match="caller-owned"):
            async with transaction(connection):
                pass
        assert connection.in_transaction
        require_transaction(connection)
        await connection.rollback()
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_transaction_releases_lock_when_connection_is_closed(tmp_path: Path) -> None:
    connection = await SqliteConnectionFactory(tmp_path / "app.db", init_schema=False).create()
    await connection.close()
    state = database._state_for(connection)

    for _ in range(2):
        with pytest.raises(ValueError):
            async with transaction(connection):
                pass
        assert not state.lock.locked()


@pytest.mark.asyncio
async def test_transaction_rejects_writes_from_non_owner_task(tmp_path: Path) -> None:
    connection = await SqliteConnectionFactory(tmp_path / "app.db", init_schema=False).create()
    try:
        async with transaction(connection):

            async def foreign_task() -> None:
                require_connection_access(connection)

            foreign = asyncio.create_task(foreign_task())
            with pytest.raises(RuntimeError, match="another asyncio task"):
                await foreign
            require_transaction(connection)
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_transaction_cancellation_rolls_back_before_releasing_connection(
    tmp_path: Path,
) -> None:
    connection = await SqliteConnectionFactory(tmp_path / "app.db", init_schema=False).create()
    entered = asyncio.Event()
    try:
        async with connection.execute("CREATE TABLE sample (value TEXT NOT NULL)"):
            pass

        async def cancelled_owner() -> None:
            async with transaction(connection):
                async with connection.execute("INSERT INTO sample VALUES ('cancelled')"):
                    pass
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(cancelled_owner())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert not connection.in_transaction
        await _insert_in_transaction(connection, "after")
        assert await _scalar(connection, "SELECT value FROM sample") == "after"
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_transaction_cancellation_while_begin_is_queued_cleans_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    connection = await SqliteConnectionFactory(tmp_path / "app.db", init_schema=False).create()
    started = ThreadEvent()
    release = ThreadEvent()
    begin_requested = asyncio.Event()
    original_execute = connection.execute

    def watched_execute(statement: str, *args: object, **kwargs: object):
        if statement == "BEGIN IMMEDIATE":
            begin_requested.set()
        return original_execute(statement, *args, **kwargs)

    monkeypatch.setattr(connection, "execute", watched_execute)

    def hold_sqlite_worker() -> int:
        started.set()
        release.wait()
        return 1

    try:
        await connection.create_function("hold_worker", 0, hold_sqlite_worker)

        async def blocked_query() -> None:
            async with connection.execute("SELECT hold_worker()") as cursor:
                await cursor.fetchone()

        query_task = asyncio.create_task(blocked_query())
        await asyncio.to_thread(started.wait)

        transaction_task = asyncio.create_task(_empty_transaction(connection))
        await begin_requested.wait()
        transaction_task.cancel()
        release.set()

        with pytest.raises(asyncio.CancelledError):
            await transaction_task
        await query_task
        assert not connection.in_transaction

        async with transaction(connection):
            pass
    finally:
        release.set()
        await connection.close()


async def _empty_transaction(connection: aiosqlite.Connection) -> None:
    async with transaction(connection):
        pass


async def _insert_in_transaction(connection: aiosqlite.Connection, value: str) -> None:
    async with (
        transaction(connection),
        connection.execute("INSERT INTO sample VALUES (?)", (value,)),
    ):
        pass


@pytest.mark.asyncio
async def test_transaction_contexts_serialize_same_connection(tmp_path: Path) -> None:
    connection = await SqliteConnectionFactory(tmp_path / "app.db", init_schema=False).create()
    first_entered = asyncio.Event()
    second_attempting = asyncio.Event()
    second_entered = asyncio.Event()
    release_first = asyncio.Event()
    order: list[str] = []
    try:
        async with connection.execute("CREATE TABLE sample (value TEXT NOT NULL)"):
            pass

        async def first() -> None:
            async with transaction(connection):
                order.append("first")
                first_entered.set()
                await release_first.wait()

        async def second() -> None:
            second_attempting.set()
            async with transaction(connection):
                second_entered.set()
                order.append("second")

        first_task = asyncio.create_task(first())
        await first_entered.wait()
        second_task = asyncio.create_task(second())
        await second_attempting.wait()
        assert not second_entered.is_set()
        release_first.set()
        await asyncio.gather(first_task, second_task)
        assert order == ["first", "second"]
    finally:
        release_first.set()
        await connection.close()
