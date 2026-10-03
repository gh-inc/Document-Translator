"""Contract checks for the SQLite DDL; repository behavior is tested elsewhere."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import BaseModel

from app.core.models import (
    BlockTranslationRecord,
    ChunkAttemptRecord,
    ChunkBlockRecord,
    ChunkRecord,
    DocumentAnalysisRecord,
    DocumentRecord,
    JobRecord,
)

ROOT = Path(__file__).resolve().parents[3]
SCHEMA_PATH = ROOT / "app" / "adapters" / "persistence" / "schema.sql"
EXPECTED_TABLES = {
    "documents",
    "blocks",
    "document_analyses",
    "jobs",
    "chunks",
    "chunk_blocks",
    "block_translations",
    "chunk_attempts",
}


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    con = sqlite3.connect(tmp_path / "schema.db")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=20000")
    yield con
    con.close()


def _table_columns(con: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(row[1] for row in con.execute(f'PRAGMA table_info("{table}")'))


def _load_schema(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))


def _seed_unique_constraint_rows(con: sqlite3.Connection) -> None:
    con.execute(
        "INSERT INTO documents (id, filename, format, size_bytes, storage_path, status) "
        "VALUES ('doc-1', 'source.pdf', 'pdf', 100, '/uploads/doc-1', 'extracted')"
    )
    con.executemany(
        "INSERT INTO blocks (id, document_id, seq, source_text, source_hash) "
        "VALUES (?, 'doc-1', ?, 'Hello', 'hash')",
        [("block-1", 1), ("block-2", 2)],
    )
    con.execute(
        "INSERT INTO jobs (id, document_id, batch_id, target_language, status, model, "
        "prompt_version, idempotency_key) "
        "VALUES ('job-1', 'doc-1', 'batch-1', 'de', 'queued', 'test', 'v1', 'key-1')"
    )
    con.executemany(
        "INSERT INTO chunks (id, job_id, seq, status) VALUES (?, 'job-1', ?, 'pending')",
        [("chunk-1", 1), ("chunk-2", 2)],
    )
    con.execute(
        "INSERT INTO block_translations (translation_key, source_hash, translated_text) "
        "VALUES ('translation-key', 'hash', 'Hallo')"
    )


def test_schema_executes_twice_and_creates_exact_tables(
    connection: sqlite3.Connection,
) -> None:
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    connection.executescript(schema)
    connection.executescript(schema)

    found = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    assert found == EXPECTED_TABLES


@pytest.mark.parametrize(
    ("table", "record_type"),
    [
        ("documents", DocumentRecord),
        ("document_analyses", DocumentAnalysisRecord),
        ("jobs", JobRecord),
        ("chunks", ChunkRecord),
        ("chunk_attempts", ChunkAttemptRecord),
        ("block_translations", BlockTranslationRecord),
        ("chunk_blocks", ChunkBlockRecord),
    ],
)
def test_record_fields_match_table_columns(
    connection: sqlite3.Connection,
    table: str,
    record_type: type[BaseModel],
) -> None:
    connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    assert tuple(record_type.model_fields) == _table_columns(connection, table)


def test_blocks_table_has_expected_columns(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    assert _table_columns(connection, "blocks") == (
        "id",
        "document_id",
        "seq",
        "source_text",
        "source_hash",
        "format_metadata",
    )


def test_translation_cache_is_addressed_by_source_hash(
    connection: sqlite3.Connection,
) -> None:
    _load_schema(connection)
    columns = list(connection.execute("PRAGMA table_info(block_translations)"))
    assert tuple(str(row[1]) for row in columns) == (
        "translation_key",
        "source_hash",
        "translated_text",
        "created_at",
    )
    assert tuple(str(row[1]) for row in sorted(columns, key=lambda row: row[5]) if row[5]) == (
        "translation_key",
        "source_hash",
    )


@pytest.mark.parametrize(
    ("index_name", "expected_columns"),
    [
        ("idx_jobs_claim", ("status", "lease_expires_at")),
        ("idx_chunks_claim", ("job_id", "status", "lease_expires_at")),
        ("idx_chunks_expired", ("status", "lease_expires_at")),
        ("idx_block_translations_lookup", ("translation_key", "source_hash")),
    ],
)
def test_claim_loop_indexes_exist_in_column_order(
    connection: sqlite3.Connection,
    index_name: str,
    expected_columns: tuple[str, ...],
) -> None:
    connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    index = connection.execute(
        "SELECT tbl_name FROM sqlite_master WHERE type='index' AND name=?",
        (index_name,),
    ).fetchone()
    assert index is not None
    columns = tuple(row[2] for row in connection.execute(f'PRAGMA index_info("{index_name}")'))
    assert columns == expected_columns


@pytest.mark.parametrize(
    ("join_setup", "duplicate_insert"),
    [
        (
            None,
            "INSERT INTO blocks (id, document_id, seq, source_text, source_hash) "
            "VALUES ('block-duplicate', 'doc-1', 1, 'Hello', 'hash')",
        ),
        (
            None,
            "INSERT INTO chunks (id, job_id, seq, status) "
            "VALUES ('chunk-duplicate', 'job-1', 1, 'pending')",
        ),
        (
            "INSERT INTO chunk_blocks (chunk_id, block_id, seq_in_chunk) "
            "VALUES ('chunk-1', 'block-1', 0)",
            "INSERT INTO chunk_blocks (chunk_id, block_id, seq_in_chunk) "
            "VALUES ('chunk-1', 'block-1', 1)",
        ),
        (
            "INSERT INTO chunk_blocks (chunk_id, block_id, seq_in_chunk) "
            "VALUES ('chunk-1', 'block-1', 0)",
            "INSERT INTO chunk_blocks (chunk_id, block_id, seq_in_chunk) "
            "VALUES ('chunk-1', 'block-2', 0)",
        ),
        (
            None,
            "INSERT INTO block_translations (translation_key, source_hash, translated_text) "
            "VALUES ('translation-key', 'hash', 'Hallo again')",
        ),
        (
            None,
            "INSERT INTO jobs (id, document_id, batch_id, target_language, status, model, "
            "prompt_version, idempotency_key) "
            "VALUES ('job-duplicate', 'doc-1', 'batch-1', 'fr', 'queued', 'test', 'v1', 'key-1')",
        ),
    ],
    ids=(
        "block-document-seq",
        "chunk-job-seq",
        "chunk-block-key",
        "chunk-block-seq",
        "translation-cache-key-source-hash",
        "job-idempotency-key",
    ),
)
def test_unique_constraints_reject_duplicate_rows(
    connection: sqlite3.Connection,
    join_setup: str | None,
    duplicate_insert: str,
) -> None:
    _load_schema(connection)
    _seed_unique_constraint_rows(connection)
    if join_setup is not None:
        connection.execute(join_setup)
    connection.commit()

    with pytest.raises(sqlite3.IntegrityError), connection:
        connection.execute(duplicate_insert)


def test_schema_constraints_cascade_and_invalid_join_rollback(
    connection: sqlite3.Connection,
) -> None:
    _load_schema(connection)
    assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
    assert connection.execute("PRAGMA busy_timeout").fetchone() == (20000,)
    assert connection.execute("PRAGMA synchronous").fetchone() == (1,)
    assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)

    connection.execute(
        "INSERT INTO documents (id, filename, format, size_bytes, storage_path, status, "
        "error_code) "
        "VALUES ('doc-1', 'source.pdf', 'pdf', 100, '/uploads/doc-1', 'extracted', NULL)"
    )
    connection.execute(
        "INSERT INTO blocks (id, document_id, seq, source_text, source_hash) "
        "VALUES ('block-1', 'doc-1', 1, 'Hello', 'hash-1')"
    )
    connection.execute(
        "INSERT INTO document_analyses "
        "(document_id, source_language, domain, register, triage_status) "
        "VALUES ('doc-1', 'en', 'general', 'neutral', 'ok')"
    )
    connection.execute(
        "INSERT INTO jobs (id, document_id, batch_id, target_language, status, model, "
        "prompt_version, idempotency_key) "
        "VALUES ('job-1', 'doc-1', 'batch-1', 'de', 'queued', 'test', 'v1', 'key-1')"
    )
    connection.execute(
        "INSERT INTO chunks (id, job_id, seq, status) VALUES ('chunk-1', 'job-1', 1, 'pending')"
    )
    connection.execute(
        "INSERT INTO chunk_attempts (id, chunk_id, attempt_no, outcome) "
        "VALUES ('attempt-1', 'chunk-1', 1, 'ok')"
    )
    connection.execute(
        "INSERT INTO block_translations (translation_key, source_hash, translated_text) "
        "VALUES ('key', 'hash-1', 'Hallo')"
    )
    connection.commit()

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"), connection:
        connection.execute(
            "INSERT INTO jobs (id, document_id, batch_id, target_language, status, model, "
            "prompt_version, idempotency_key) "
            "VALUES ('job-rollback', 'doc-1', 'batch-1', 'fr', 'queued', 'test', 'v1', "
            "'key-rollback')"
        )
        connection.execute(
            "INSERT INTO chunks (id, job_id, seq, status) "
            "VALUES ('chunk-rollback', 'job-rollback', 1, 'pending')"
        )
        connection.execute(
            "INSERT INTO chunk_blocks (chunk_id, block_id, seq_in_chunk) "
            "VALUES ('chunk-rollback', 'block-1', 0)"
        )
        connection.execute(
            "INSERT INTO chunk_blocks (chunk_id, block_id, seq_in_chunk) "
            "VALUES ('chunk-rollback', 'missing-block', 1)"
        )

    assert connection.execute("SELECT COUNT(*) FROM chunk_blocks").fetchone() == (0,)
    assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone() == (1,)
    assert connection.execute("SELECT COUNT(*) FROM chunks").fetchone() == (1,)

    connection.execute(
        "INSERT INTO chunk_blocks (chunk_id, block_id, seq_in_chunk) "
        "VALUES ('chunk-1', 'block-1', 0)"
    )

    connection.execute("DELETE FROM documents WHERE id='doc-1'")
    for table in (
        "blocks",
        "document_analyses",
        "jobs",
        "chunks",
        "chunk_blocks",
        "chunk_attempts",
    ):
        assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
    # Translation memory is content-addressed and outlives source documents.
    assert connection.execute("SELECT COUNT(*) FROM block_translations").fetchone() == (1,)
