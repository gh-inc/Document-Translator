# Stage 1 — Persistence Layer Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement a working SQLite persistence adapter and filesystem storage so that the core repository ports have concrete, tested implementations.

**Architecture:**
- Use **direct `aiosqlite`** with raw SQL; `schema.sql` is the single source of DDL truth.
- Repositories receive an **active `aiosqlite.Connection`** in `__init__`; they never open/close connections themselves. Transaction lifecycle is managed by the factory/application layer.
- All SQL uses **parameterized `?` placeholders**; string concatenation/f-strings for SQL are forbidden.
- Datetimes are serialized as **UTC ISO 8601 strings** (`datetime.now(timezone.utc).isoformat()`) to avoid timezone bugs in lease expiry checks.
- `FilesystemStorage` prevents path traversal by resolving paths with `os.path.abspath` and verifying they remain under the configured base directory.

**Tech Stack:** Python 3.12, `aiosqlite`, Pydantic v2.

**Current State:**
- `app/core/models.py` and `app/core/ports.py` are already finalized.
- `app/adapters/persistence/schema.sql` and `tests/adapters/persistence/test_schema.py` already exist and pass.
- `app/adapters/storage/filesystem.py` is an unwired skeleton.

## Execution Decomposition and Corrections

- **DT-11:** minimal settings and environment-override tests.
- **DT-12:** connection factory and explicit transaction context for ordinary
  application/service writes; per-connection serialization and rollback tests.
- **DT-13:** document, execution, and cache repositories with all port methods.
- **DT-14:** filesystem storage and path-containment tests.
- **DT-15:** independent integration/concurrency/recovery validation and docs.

DT-7–DT-9 are already complete and are not reused for new implementation work.
The main agent coordinates three workers with separate file ownership because
`superpowers:executing-plans` is not installed in this session.

The following internal implementation corrections keep the approved core ports
and schema unchanged:

1. WAL tests use a file-backed temporary database. SQLite in-memory databases
   report `memory`, not `wal`; they remain supported for isolated tests.
2. Cursor `fetchone()` and `fetchall()` are awaited, and cursors are closed with
   async context managers. Schema/file reads and filesystem work use
   `asyncio.to_thread`.
3. Ordinary mutations require an active caller transaction. The persistence
   adapter exposes `transaction(connection)` for application/service callers;
   aggregate enqueue owns this context itself. It commits or rolls back the
   entire aggregate on failure before COMMIT, including cancellation, and rejects nested caller-owned
   transactions without rolling them back. Same-connection transaction contexts
   are serialized, and another task cannot read or write inside the current owner's
   managed transaction. Manual BEGIN callers must serialize their own use. Once
   COMMIT starts it is shielded to completion; cancellation delivered at that
   point is consumed on a successful commit, which is reported as success. A
   failed COMMIT still raises its error and triggers rollback.
4. Claims also recover expired running/assembling jobs as required by
   ARCHITECTURE.md's “State machines”; reclaiming assembling preserves that
   status. Progress remains monotonic. Attempt insertion and cumulative job
   usage/cost updates occur in the same caller transaction.
5. Datetimes are serialized in UTC with fixed microsecond precision so SQL lease
   comparisons are consistent. Naive input dates are interpreted as UTC, never
   as the machine's local timezone.
6. Filesystem checks use `os.path.abspath`, canonical resolution, and path
   components rather than string prefixes. Both IDs and filenames are single
   components, and canonical symlink escapes are rejected.
7. Worker composition can bind an execution repository to `worker_id` through
   an optional concrete-adapter constructor keyword. Scoped claims and state
   mutations are fenced to the current, unexpired worker lease; chunk mutations
   also check the parent job lease. Default instances are unscoped for enqueue
   and administrative operations. Existing core port signatures stay unchanged.
   Attempt spend is always recorded, even when the caller's lease was lost.
   Each concurrently active worker incarnation must use a distinct worker ID.

---

## Task 1: Create Minimal `app/config.py`

**Files:**
- Create: `app/config.py`

Add a Pydantic Settings class with:

```python
class Settings(BaseSettings):
    database_path: Path = Path("/data/app.db")
    upload_storage_path: Path = Path("/data/uploads")
    output_storage_path: Path = Path("/data/out")
```

Use `pydantic_settings` (already in dependencies). Defaults must work for tests via overrides and for Docker via env.

**Step 1: Write the test**

```python
def test_settings_have_defaults() -> None:
    settings = Settings()
    assert settings.database_path == Path("/data/app.db")
```

Run: `pytest tests/test_config.py -v`
Expected: FAIL before implementation.

**Step 2: Implement `app/config.py`**

**Step 3: Run test**

Expected: PASS.

---

## Task 2: Create SQLite Connection Factory

**Files:**
- Create: `app/adapters/persistence/database.py`

Implement `SqliteConnectionFactory`:

```python
class SqliteConnectionFactory:
    def __init__(self, db_path: Path, *, init_schema: bool = True) -> None: ...

    async def create(self) -> aiosqlite.Connection:
        """Open a connection, apply pragmas, optionally load schema, return it."""
        ...
```

On every new connection execute:

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=20000;
```

If `init_schema` is true, run `schema.sql` via `executescript`.

**Step 1: Write the test**

```python
async def test_factory_applies_wal_pragmas_and_loads_schema(tmp_path: Path) -> None:
    factory = SqliteConnectionFactory(tmp_path / "app.db")
    conn = await factory.create()
    try:
        async with conn.execute("PRAGMA journal_mode") as cursor:
            assert tuple(await cursor.fetchone()) == ("wal",)
        async with conn.execute("PRAGMA foreign_keys") as cursor:
            assert tuple(await cursor.fetchone()) == (1,)
        async with conn.execute("PRAGMA busy_timeout") as cursor:
            assert tuple(await cursor.fetchone()) == (20000,)
        async with conn.execute("PRAGMA synchronous") as cursor:
            assert tuple(await cursor.fetchone()) == (1,)
        async with conn.execute("SELECT name FROM sqlite_master WHERE type='table'") as cursor:
            tables = {row[0] for row in await cursor.fetchall()}
        assert "documents" in tables
    finally:
        await conn.close()
```

Run: `pytest tests/adapters/persistence/test_database.py -v`
Expected: FAIL.

**Step 2: Implement `database.py`**

**Step 3: Run test**

Expected: PASS.

---

## Task 3: Implement SQLite Repositories

**Files:**
- Create: `app/adapters/persistence/repositories.py`

Implement three classes:

```python
class SqliteDocumentRepository:
    def __init__(self, connection: aiosqlite.Connection) -> None: ...


class SqliteJobExecutionRepository:
    def __init__(self, connection: aiosqlite.Connection) -> None: ...


class SqliteTranslationCacheRepository:
    def __init__(self, connection: aiosqlite.Connection) -> None: ...
```

Each repository method uses `self._connection.execute(...)` with `?` placeholders only.

**Key requirements:**

- `SqliteDocumentRepository.create_document`, `get_document`, `update_document_status`, `create_blocks`, `get_blocks`, `save_analysis`, `get_analysis`.
- `SqliteJobExecutionRepository.create_job_with_chunks` runs inside one transaction:
  - `BEGIN`
  - insert job
  - insert chunks
  - insert chunk_blocks
  - `COMMIT` on success; `ROLLBACK` on any exception.
- `SqliteJobExecutionRepository.claim_job` uses atomic `UPDATE ... RETURNING`:
  ```sql
  UPDATE jobs
  SET status = 'running', lease_owner = ?, lease_expires_at = ?
  WHERE id = (
      SELECT id FROM jobs
      WHERE status = 'queued'
        AND (lease_expires_at IS NULL OR lease_expires_at < ?)
      ORDER BY created_at ASC
      LIMIT 1
  )
  RETURNING *;
  ```
- `claim_chunk` uses the same pattern on `chunks` for `status = 'pending'`.
- `release_expired_chunks` resets `inflight` chunks with `lease_expires_at < ?` to `pending` and returns them.
- `TranslationCacheRepository.save_block_translation` uses `INSERT OR IGNORE` + read-back.

**Step 1: Write a failing repository contract test**

Create `tests/adapters/persistence/test_repositories.py` with a fixture that opens an in-memory connection, applies the factory, and instantiates all three repositories:

```python
@pytest.fixture
async def repositories(tmp_path: Path):
    factory = SqliteConnectionFactory(Path(":memory:"))
    conn = await factory.create()
    yield (
        SqliteDocumentRepository(conn),
        SqliteJobExecutionRepository(conn),
        SqliteTranslationCacheRepository(conn),
    )
    await conn.close()
```

Add at least one failing test:

```python
async def test_create_job_with_chunks_is_atomic(repositories) -> None:
    doc_repo, job_repo, _ = repositories
    await doc_repo.create_document(...)
    await doc_repo.create_blocks(...)
    # Expect NotImplementedError or missing class before implementation
```

Run: `pytest tests/adapters/persistence/test_repositories.py -v`
Expected: FAIL.

**Step 2: Implement the repositories**

Use JSON serialization for `format_metadata`, `glossary`, `terms`, `warnings`:

```python
json.dumps(value, ensure_ascii=False)
```

Use UTC ISO strings for datetimes:

```python
value.astimezone(timezone.utc).isoformat()
```

**Step 3: Run contract tests**

Expected: PASS.

---

## Task 4: Implement Filesystem Storage

**Files:**
- Modify: `app/adapters/storage/filesystem.py`

Replace the skeleton with:

```python
class FilesystemStorage(FileStorage):
    def __init__(self, upload_base: Path, output_base: Path) -> None: ...
```

Methods:

- `save_upload(document_id, content, filename)` → write to `<upload_base>/<document_id>/filename`, return path.
- `get_upload_path(document_id)` → return path if it exists under `<upload_base>/<document_id>`.
- `save_output(job_id, content, filename)` → write to `<output_base>/<job_id>/filename`, return path.
- `get_output_path(job_id)` → return path if it exists under `<output_base>/<job_id>`.

Security helper:

```python
def _resolve(base: Path, *parts: str) -> Path:
    canonical_base = Path(os.path.abspath(base)).resolve()
    target = Path(os.path.abspath(canonical_base.joinpath(*parts))).resolve()
    if not target.is_relative_to(canonical_base):
        raise ValueError("path traversal detected")
    return target
```

**Step 1: Write the failing test**

```python
async def test_filesystem_storage_roundtrips_upload() -> None:
    storage = FilesystemStorage(Path("/tmp/uploads"), Path("/tmp/out"))
    path = await storage.save_upload("doc-1", b"hello", "source.pdf")
    assert path.read_bytes() == b"hello"
```

Run: `pytest tests/adapters/storage/test_filesystem.py -v`
Expected: FAIL.

**Step 2: Implement `FilesystemStorage`**

**Step 3: Run tests**

Expected: PASS.

---

## Task 5: Expand Repository Contract Tests

**Files:**
- Modify: `tests/adapters/persistence/test_repositories.py`

Add coverage for:

- Document lifecycle (create, get, update status, create blocks, get blocks, save/get analysis).
- Job lifecycle (create with chunks + chunk_blocks, get, claim, heartbeat, progress, complete).
- Chunk lifecycle (pending, claim, heartbeat, complete, release expired).
- Atomic rollback: simulate an error mid-`create_job_with_chunks` and assert no partial rows.
- Cache exactly-once: save twice, get once.

Run: `pytest tests/adapters/persistence/test_repositories.py -v`
Expected: PASS.

---

## Task 6: Add Path-Traversal Tests

**Files:**
- Modify: `tests/adapters/storage/test_filesystem.py`

Add:

```python
async def test_filesystem_storage_rejects_path_traversal() -> None:
    storage = FilesystemStorage(Path("/tmp/uploads"), Path("/tmp/out"))
    with pytest.raises(ValueError):
        await storage.save_upload("../etc", b"x", "passwd")
```

Run: `pytest tests/adapters/storage/test_filesystem.py -v`
Expected: PASS.

---

## Task 7: Update `TASKS.md`

**Files:**
- Modify: `TASKS.md`

Maintain DT-11–DT-15 for this stage. DT-7–DT-9 remain completed historical tasks.

---

## Task 8: Full Verification

```bash
make test
make lint
make typecheck
```

Expected: all green.

---

## Task 9: Commit

```bash
git add app/config.py app/adapters/persistence app/adapters/storage tests TASKS.md
git commit -m "DT-13: feat(persistence): implement SQLite repository ports"
```

Create separate commits for DT-11, DT-12, DT-13, DT-14, and DT-15 with their
corresponding implementation/tests and documentation rather than committing the
entire stage under the old DT-9 ticket.

---

## Final File List

**Created:**
- `app/config.py`
- `app/adapters/persistence/database.py`
- `app/adapters/persistence/repositories.py`
- `tests/adapters/persistence/test_database.py`
- `tests/adapters/persistence/test_repositories.py`
- `tests/adapters/storage/test_filesystem.py`
- `tests/test_config.py`
- `tests/adapters/persistence/test_persistence_integration.py`

**Modified:**
- `app/adapters/storage/filesystem.py`
- `TASKS.md`
- `PROMPTS.md`

## Execution Results

All five tasks were implemented with separate ownership and independently
reviewed. The main agent added integration tests using two real WAL connections
for claims, idempotent enqueue, cache insertion, attempt accounting, recovery,
and cancellation. The read-only `persistence_review` agent identified connection
ownership and stale-worker mutation risks; the workers implemented guards and
the reviewer confirmed the fixes.

Filesystem storage publishes one artifact per record. Saving the same filename
atomically replaces it; another filename raises `FileExistsError`. Temporary
files are excluded from lookup. Traversal and symlink aliases are rejected.

Validation: `make test` passed **145 tests**, `make lint` and `make typecheck`
passed. The two existing Pydantic warnings about approved `register` fields
remain. Real aiosqlite and thread-backed filesystem tests ran outside the tool
sandbox because its execution environment stalled their worker threads; no
thread mocks or real provider calls were used for the acceptance run.

These adapters are ready for composition by application startup and workers.
REST endpoints, worker loops, and their dependency wiring belong to later
stages. Worker execution repositories must opt into the concrete `worker_id`
binding described above to enforce lease ownership on state mutations.
