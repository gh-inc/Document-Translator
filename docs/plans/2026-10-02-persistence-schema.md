# Persistence Schema Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Create `app/adapters/persistence/schema.sql` with the full SQLite DDL for all tables defined in ARCHITECTURE.md's “Data model (SQLite, WAL)”, plus indexes optimized for the worker claim loop.

**Architecture:** SQL lives exclusively in the persistence adapter. WAL pragmas (`journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`, `busy_timeout=20000`) are applied by the application at startup, not in this file.

**Tech Stack:** SQLite 3, aiosqlite (runtime).

**Status:** Implemented and validated under DT-9. All 20 schema tests pass;
the combined suite passes 79 tests, and `make lint` / `make typecheck` are clean.

---

## Task 1: Create the Persistence Package

**Files:**
- Create: `app/adapters/persistence/__init__.py`

Keep `__init__.py` minimal (empty or a docstring). The schema file will be loaded by repository code later.

```python
"""SQLite persistence adapter."""
```

---

## Task 2: Write `schema.sql`

**Files:**
- Create: `app/adapters/persistence/schema.sql`

Include all tables and indexes as approved. The DDL is provided below in the "Final Code" section.

**Step 1: Verify SQL syntax**

```bash
python - <<'PY'
import sqlite3
schema = open("app/adapters/persistence/schema.sql").read()
con = sqlite3.connect(":memory:")
con.executescript(schema)
print("schema OK")
PY
```

Expected output: `schema OK`

---

## Task 3: Add a Schema Load Test

**Files:**
- Create: `tests/adapters/persistence/test_schema.py`

Verify that the schema executes cleanly on an in-memory SQLite connection and that the claim-loop indexes exist.

```python
import sqlite3

from pathlib import Path

SCHEMA_PATH = (
    Path(__file__).resolve().parents[3] / "app" / "adapters" / "persistence" / "schema.sql"
)


def test_schema_loads_without_errors() -> None:
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA_PATH.read_text())
    con.close()


def test_claim_loop_indexes_exist() -> None:
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA_PATH.read_text())
    cur = con.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name IN (?, ?, ?, ?)",
        (
            "idx_jobs_claim",
            "idx_chunks_claim",
            "idx_chunks_expired",
            "idx_block_translations_lookup",
        ),
    )
    found = {row[0] for row in cur.fetchall()}
    con.close()
    assert found == {
        "idx_jobs_claim",
        "idx_chunks_claim",
        "idx_chunks_expired",
        "idx_block_translations_lookup",
    }
```

Run: `pytest tests/adapters/persistence/test_schema.py -v`
Expected: PASS

---

## Task 4: Run Full Verification

```bash
make test
make lint
make typecheck
```

Expected: all green.

---

## Task 5: Commit

```bash
git add app/adapters/persistence/ tests/adapters/persistence/test_schema.py
git commit -m "DT-9: feat(persistence): add SQLite schema with claim-loop indexes"
```

---

## Final Code for `app/adapters/persistence/schema.sql`

**Execution notes:** DT-9 preserves the approved DDL. Schema tests use file-backed
temporary databases, applying all four required connection PRAGMAs explicitly;
WAL cannot be exercised with an in-memory database. Tests validate table/record
parity, index columns, uniqueness, foreign keys, cascades, and transactional
rollback. These tests validate the schema; repository methods and application
startup wiring are later stages. The main agent coordinates this task with the
available collaboration tools because `superpowers:executing-plans` is unavailable.

```sql
-- Schema for the SQLite persistence adapter.
-- WAL, synchronous and busy_timeout are set by the application at startup;
-- this file contains only DDL.

CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    format TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    page_count INTEGER,
    storage_path TEXT NOT NULL,
    status TEXT NOT NULL,
    error_code TEXT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS blocks (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    source_text TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    format_metadata TEXT NOT NULL DEFAULT '{}',
    UNIQUE(document_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_blocks_document_id_seq ON blocks(document_id, seq);

CREATE TABLE IF NOT EXISTS document_analyses (
    document_id TEXT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
    source_language TEXT NOT NULL,
    domain TEXT NOT NULL,
    register TEXT NOT NULL,
    terms TEXT NOT NULL DEFAULT '[]',
    warnings TEXT NOT NULL DEFAULT '[]',
    triage_status TEXT NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    batch_id TEXT NOT NULL,
    target_language TEXT NOT NULL,
    status TEXT NOT NULL,
    total_chunks INTEGER NOT NULL DEFAULT 0,
    done_chunks INTEGER NOT NULL DEFAULT 0,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    glossary TEXT NOT NULL DEFAULT '{}',
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0.0,
    error_code TEXT,
    error_detail TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,
    lease_owner TEXT,
    lease_expires_at DATETIME,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Claim loop: find the oldest queued/running/assembling job with an expired or missing lease.
CREATE INDEX IF NOT EXISTS idx_jobs_claim ON jobs(status, lease_expires_at);

-- Batch lookup.
CREATE INDEX IF NOT EXISTS idx_jobs_batch_id ON jobs(batch_id);

-- Idempotency lookup.
CREATE INDEX IF NOT EXISTS idx_jobs_idempotency_key ON jobs(idempotency_key);

CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    status TEXT NOT NULL,
    lease_owner TEXT,
    lease_expires_at DATETIME,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(job_id, seq)
);

-- Claim loop: pending chunks for a job, ordered by seq, preferring unleased or expired.
CREATE INDEX IF NOT EXISTS idx_chunks_claim ON chunks(job_id, status, lease_expires_at);

-- Reclaim loop: any inflight chunk with an expired lease.
CREATE INDEX IF NOT EXISTS idx_chunks_expired ON chunks(status, lease_expires_at);

CREATE TABLE IF NOT EXISTS chunk_blocks (
    chunk_id TEXT NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    block_id TEXT NOT NULL REFERENCES blocks(id) ON DELETE CASCADE,
    seq_in_chunk INTEGER NOT NULL,
    PRIMARY KEY (chunk_id, block_id),
    UNIQUE(chunk_id, seq_in_chunk)
);

CREATE INDEX IF NOT EXISTS idx_chunk_blocks_block_id ON chunk_blocks(block_id);

CREATE TABLE IF NOT EXISTS block_translations (
    translation_key TEXT NOT NULL,
    block_id TEXT NOT NULL REFERENCES blocks(id) ON DELETE CASCADE,
    translated_text TEXT NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (translation_key, block_id)
);

-- Cache lookup by key + block.
CREATE INDEX IF NOT EXISTS idx_block_translations_lookup ON block_translations(translation_key, block_id);

CREATE TABLE IF NOT EXISTS chunk_attempts (
    id TEXT PRIMARY KEY,
    chunk_id TEXT NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    attempt_no INTEGER NOT NULL,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0.0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    outcome TEXT NOT NULL,
    error_detail TEXT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_chunk_attempts_chunk_id ON chunk_attempts(chunk_id);
```
