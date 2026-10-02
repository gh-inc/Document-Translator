"""SQLite implementations of the core persistence ports."""

from __future__ import annotations

# Lease comparisons are inclusive at expiry (``expires_at <= now``). Stored and
# compared timestamps are fixed-width UTC ISO 8601 strings for stable ordering.
import json
from datetime import UTC, datetime
from typing import cast

import aiosqlite

from app.adapters.persistence.database import (
    require_connection_access,
    require_transaction,
    transaction,
)
from app.core.models import (
    Block,
    ChunkAttemptRecord,
    ChunkBlockRecord,
    ChunkRecord,
    DocumentAnalysisRecord,
    DocumentRecord,
    DocumentStatus,
    JobError,
    JobRecord,
    JobStatus,
    TranslationPlan,
)


def _utc(value: datetime) -> datetime:
    """Normalize a timestamp to UTC; naive inputs are explicitly treated as UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds")


def _row_dict(
    cursor: aiosqlite.Cursor,
    row: aiosqlite.Row | tuple[object, ...],
) -> dict[str, object]:
    if cursor.description is None:
        raise RuntimeError("query did not return columns")
    return {column[0]: row[index] for index, column in enumerate(cursor.description)}


async def _fetch_one(
    connection: aiosqlite.Connection,
    sql: str,
    parameters: tuple[object, ...] = (),
) -> dict[str, object] | None:
    require_connection_access(connection)
    async with connection.execute(sql, parameters) as cursor:
        row = await cursor.fetchone()
        return None if row is None else _row_dict(cursor, row)


async def _fetch_all(
    connection: aiosqlite.Connection,
    sql: str,
    parameters: tuple[object, ...] = (),
) -> list[dict[str, object]]:
    require_connection_access(connection)
    async with connection.execute(sql, parameters) as cursor:
        rows = await cursor.fetchall()
        return [_row_dict(cursor, row) for row in rows]


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def _decoded(value: object) -> object:
    if not isinstance(value, str):
        raise TypeError("database JSON column was not stored as text")
    return json.loads(value)


def _with_utc_dates(row: dict[str, object], *keys: str) -> dict[str, object]:
    for key in keys:
        value = row[key]
        if isinstance(value, datetime):
            row[key] = _utc(value)
        elif isinstance(value, str):
            parsed = datetime.fromisoformat(value)
            row[key] = _utc(parsed)
    return row


def _document(row: dict[str, object]) -> DocumentRecord:
    _with_utc_dates(row, "created_at")
    return DocumentRecord.model_validate(row)


def _analysis(row: dict[str, object]) -> DocumentAnalysisRecord:
    row["terms"] = _decoded(row["terms"])
    row["warnings"] = _decoded(row["warnings"])
    _with_utc_dates(row, "created_at")
    return DocumentAnalysisRecord.model_validate(row)


def _job(row: dict[str, object]) -> JobRecord:
    row["glossary"] = _decoded(row["glossary"])
    _with_utc_dates(row, "created_at", "updated_at", "lease_expires_at")
    return JobRecord.model_validate(row)


def _chunk(row: dict[str, object]) -> ChunkRecord:
    _with_utc_dates(row, "created_at", "lease_expires_at")
    return ChunkRecord.model_validate(row)


class SqliteDocumentRepository:
    """Persist documents, ordered blocks, and the first saved analysis."""

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    async def create_document(
        self,
        id: str,
        filename: str,
        format: str,
        size_bytes: int,
        storage_path: str,
        page_count: int | None = None,
    ) -> DocumentRecord:
        require_transaction(self._connection)
        created_at = _timestamp(datetime.now(UTC))
        async with self._connection.execute(
            "INSERT INTO documents "
            "(id, filename, format, size_bytes, page_count, storage_path, status, "
            "error_code, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING *",
            (
                id,
                filename,
                format,
                size_bytes,
                page_count,
                storage_path,
                DocumentStatus.UPLOADED.value,
                None,
                created_at,
            ),
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                raise RuntimeError("document insert returned no row")
            return _document(_row_dict(cursor, row))

    async def get_document(self, document_id: str) -> DocumentRecord | None:
        row = await _fetch_one(
            self._connection,
            "SELECT * FROM documents WHERE id = ?",
            (document_id,),
        )
        return None if row is None else _document(row)

    async def update_document_status(
        self,
        document_id: str,
        status: DocumentStatus,
        error_code: str | None = None,
    ) -> None:
        require_transaction(self._connection)
        async with self._connection.execute(
            "UPDATE documents SET status = ?, error_code = ? WHERE id = ?",
            (status.value, error_code, document_id),
        ):
            pass

    async def create_blocks(self, document_id: str, blocks: list[Block]) -> None:
        require_transaction(self._connection)
        async with self._connection.executemany(
            "INSERT INTO blocks "
            "(id, document_id, seq, source_text, source_hash, format_metadata) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    block.id,
                    document_id,
                    block.seq,
                    block.source_text,
                    block.source_hash,
                    _json(block.format_metadata),
                )
                for block in blocks
            ],
        ):
            pass

    async def get_blocks(self, document_id: str) -> list[Block]:
        rows = await _fetch_all(
            self._connection,
            "SELECT id, seq, source_text, source_hash, format_metadata "
            "FROM blocks WHERE document_id = ? ORDER BY seq ASC",
            (document_id,),
        )
        blocks: list[Block] = []
        for row in rows:
            row["format_metadata"] = _decoded(row["format_metadata"])
            blocks.append(Block.model_validate(row))
        return blocks

    async def save_analysis(
        self,
        document_id: str,
        plan: TranslationPlan,
    ) -> DocumentAnalysisRecord:
        require_transaction(self._connection)
        created_at = _timestamp(datetime.now(UTC))
        async with self._connection.execute(
            "INSERT OR IGNORE INTO document_analyses "
            "(document_id, source_language, domain, register, terms, warnings, "
            "triage_status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                document_id,
                plan.source_language,
                plan.domain,
                plan.register,
                _json(plan.terms),
                _json(plan.warnings),
                plan.triage_status.value,
                created_at,
            ),
        ):
            pass
        row = await _fetch_one(
            self._connection,
            "SELECT * FROM document_analyses WHERE document_id = ?",
            (document_id,),
        )
        if row is None:
            raise RuntimeError("analysis insert/read-back returned no row")
        return _analysis(row)

    async def get_analysis(self, document_id: str) -> DocumentAnalysisRecord | None:
        row = await _fetch_one(
            self._connection,
            "SELECT * FROM document_analyses WHERE document_id = ?",
            (document_id,),
        )
        return None if row is None else _analysis(row)


class SqliteJobExecutionRepository:
    """Persist jobs/chunks, optionally fencing mutations to one worker.

    ``worker_id=None`` keeps the unscoped administrative behavior. A bound
    worker can mutate only jobs and chunks carrying its unexpired leases.
    """

    def __init__(
        self,
        connection: aiosqlite.Connection,
        *,
        worker_id: str | None = None,
    ) -> None:
        self._connection = connection
        self._worker_id = worker_id

    def _claim_owner(self, worker_id: str) -> str:
        if self._worker_id is not None and worker_id != self._worker_id:
            raise ValueError("claim worker_id must match the repository's bound worker_id")
        return worker_id

    async def create_job_with_chunks(
        self,
        job: JobRecord,
        chunks: list[ChunkRecord],
        chunk_blocks: list[ChunkBlockRecord],
    ) -> None:
        async with transaction(self._connection):
            existing = await _fetch_one(
                self._connection,
                "SELECT id FROM jobs WHERE idempotency_key = ?",
                (job.idempotency_key,),
            )
            if existing is not None:
                return

            await self._validate_new_job(job)

            if len(chunks) != job.total_chunks:
                raise ValueError("job.total_chunks must equal the number of supplied chunks")
            chunk_ids = {chunk.id for chunk in chunks}
            chunk_sequences = {chunk.seq for chunk in chunks}
            if len(chunk_ids) != len(chunks) or len(chunk_sequences) != len(chunks):
                raise ValueError("chunk ids and sequence numbers must be unique")
            if any(chunk.job_id != job.id for chunk in chunks):
                raise ValueError("every chunk must belong to the supplied job")
            if any(link.chunk_id not in chunk_ids for link in chunk_blocks):
                raise ValueError("every chunk-block link must refer to a supplied chunk")

            for link in chunk_blocks:
                block = await _fetch_one(
                    self._connection,
                    "SELECT document_id FROM blocks WHERE id = ?",
                    (link.block_id,),
                )
                if block is None or block["document_id"] != job.document_id:
                    raise ValueError("every linked block must belong to the job document")

            await self._insert_job(job)
            for chunk in chunks:
                await self._insert_chunk(chunk)
            async with self._connection.executemany(
                "INSERT INTO chunk_blocks (chunk_id, block_id, seq_in_chunk) VALUES (?, ?, ?)",
                [(link.chunk_id, link.block_id, link.seq_in_chunk) for link in chunk_blocks],
            ):
                pass

    async def _validate_new_job(self, job: JobRecord) -> None:
        """Adapter extension point for serialized aggregate preconditions."""

    async def _insert_job(self, job: JobRecord) -> None:
        async with self._connection.execute(
            "INSERT INTO jobs "
            "(id, document_id, batch_id, target_language, status, total_chunks, done_chunks, "
            "model, prompt_version, glossary, tokens_in, tokens_out, cost_usd, error_code, "
            "error_detail, idempotency_key, lease_owner, lease_expires_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                job.id,
                job.document_id,
                job.batch_id,
                job.target_language,
                job.status.value,
                job.total_chunks,
                job.done_chunks,
                job.model,
                job.prompt_version,
                _json(job.glossary),
                job.tokens_in,
                job.tokens_out,
                job.cost_usd,
                job.error_code,
                job.error_detail,
                job.idempotency_key,
                job.lease_owner,
                None if job.lease_expires_at is None else _timestamp(job.lease_expires_at),
                _timestamp(job.created_at),
                _timestamp(job.updated_at),
            ),
        ):
            pass

    async def _insert_chunk(self, chunk: ChunkRecord) -> None:
        async with self._connection.execute(
            "INSERT INTO chunks "
            "(id, job_id, seq, status, lease_owner, lease_expires_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                chunk.id,
                chunk.job_id,
                chunk.seq,
                chunk.status.value,
                chunk.lease_owner,
                None if chunk.lease_expires_at is None else _timestamp(chunk.lease_expires_at),
                _timestamp(chunk.created_at),
            ),
        ):
            pass

    async def get_job(self, job_id: str) -> JobRecord | None:
        row = await _fetch_one(self._connection, "SELECT * FROM jobs WHERE id = ?", (job_id,))
        return None if row is None else _job(row)

    async def claim_job(
        self,
        worker_id: str,
        lease_expires_at: datetime,
    ) -> JobRecord | None:
        require_transaction(self._connection)
        now = _timestamp(datetime.now(UTC))
        lease = _timestamp(lease_expires_at)
        owner = self._claim_owner(worker_id)
        row = await _fetch_one(
            self._connection,
            "UPDATE jobs SET "
            "status = CASE WHEN status = 'assembling' THEN 'assembling' ELSE 'running' END, "
            "lease_owner = ?, lease_expires_at = ?, updated_at = ? "
            "WHERE id = ("
            "SELECT id FROM jobs WHERE ("
            "(status = 'queued' AND (lease_expires_at IS NULL OR lease_expires_at <= ?)) OR "
            "(status IN ('running', 'assembling') "
            "AND (lease_expires_at IS NULL OR lease_expires_at <= ?))"
            ") ORDER BY created_at ASC, id ASC LIMIT 1) "
            "RETURNING *",
            (owner, lease, now, now, now),
        )
        return None if row is None else _job(row)

    async def heartbeat_job(self, job_id: str, lease_expires_at: datetime) -> None:
        require_transaction(self._connection)
        lease = _timestamp(lease_expires_at)
        now = _timestamp(datetime.now(UTC))
        async with self._connection.execute(
            "UPDATE jobs SET lease_expires_at = CASE "
            "WHEN lease_expires_at IS NULL OR lease_expires_at < ? THEN ? "
            "ELSE lease_expires_at END, updated_at = ? "
            "WHERE id = ? AND status IN ('running', 'assembling') "
            "AND (? IS NULL OR (lease_owner = ? AND lease_expires_at > ?))",
            (lease, lease, now, job_id, self._worker_id, self._worker_id, now),
        ):
            pass

    async def update_job_progress(self, job_id: str, done_chunks: int) -> None:
        require_transaction(self._connection)
        now = _timestamp(datetime.now(UTC))
        async with self._connection.execute(
            "UPDATE jobs SET done_chunks = MIN(total_chunks, MAX(done_chunks, ?)), "
            "updated_at = ? WHERE id = ? AND status IN ('running', 'assembling') "
            "AND (? IS NULL OR (lease_owner = ? AND lease_expires_at > ?))",
            (done_chunks, now, job_id, self._worker_id, self._worker_id, now),
        ):
            pass

    async def complete_job(
        self,
        job_id: str,
        status: JobStatus,
        error: JobError | None = None,
    ) -> None:
        require_transaction(self._connection)
        if status not in {
            JobStatus.DONE,
            JobStatus.COMPLETED_WITH_ERRORS,
            JobStatus.FAILED,
            JobStatus.ASSEMBLING,
        }:
            raise ValueError("complete_job requires assembling or a terminal job status")
        if status is JobStatus.ASSEMBLING:
            now = _timestamp(datetime.now(UTC))
            async with self._connection.execute(
                "UPDATE jobs SET status = 'assembling', error_code = ?, error_detail = ?, "
                "updated_at = ? WHERE id = ? AND status = 'running' "
                "AND (? IS NULL OR (lease_owner = ? AND lease_expires_at > ?))",
                (
                    None if error is None else error.error_code,
                    None if error is None else error.message,
                    now,
                    job_id,
                    self._worker_id,
                    self._worker_id,
                    now,
                ),
            ):
                pass
            return
        now = _timestamp(datetime.now(UTC))
        async with self._connection.execute(
            "UPDATE jobs SET status = ?, error_code = ?, error_detail = ?, "
            "lease_owner = NULL, lease_expires_at = NULL, updated_at = ? "
            "WHERE id = ? AND status IN ('queued', 'running', 'assembling') "
            "AND (? IS NULL OR (lease_owner = ? AND lease_expires_at > ?))",
            (
                status.value,
                None if error is None else error.error_code,
                None if error is None else error.message,
                now,
                job_id,
                self._worker_id,
                self._worker_id,
                now,
            ),
        ):
            pass

    async def get_jobs_by_batch(self, batch_id: str) -> list[JobRecord]:
        rows = await _fetch_all(
            self._connection,
            "SELECT * FROM jobs WHERE batch_id = ? ORDER BY created_at ASC, id ASC",
            (batch_id,),
        )
        return [_job(row) for row in rows]

    async def get_pending_chunks(self, job_id: str) -> list[ChunkRecord]:
        rows = await _fetch_all(
            self._connection,
            "SELECT * FROM chunks WHERE job_id = ? AND status = 'pending' ORDER BY seq ASC, id ASC",
            (job_id,),
        )
        return [_chunk(row) for row in rows]

    async def claim_chunk(
        self,
        job_id: str,
        worker_id: str,
        lease_expires_at: datetime,
    ) -> ChunkRecord | None:
        require_transaction(self._connection)
        now = _timestamp(datetime.now(UTC))
        owner = self._claim_owner(worker_id)
        row = await _fetch_one(
            self._connection,
            "UPDATE chunks SET status = 'inflight', lease_owner = ?, lease_expires_at = ? "
            "WHERE id = (SELECT chunk.id FROM chunks AS chunk "
            "WHERE chunk.job_id = ? AND chunk.status = 'pending' "
            "AND (chunk.lease_expires_at IS NULL OR chunk.lease_expires_at <= ?) "
            "AND (? IS NULL OR EXISTS (SELECT 1 FROM jobs "
            "WHERE jobs.id = chunk.job_id AND jobs.status = 'running' "
            "AND jobs.lease_owner = ? AND jobs.lease_expires_at > ?)) "
            "ORDER BY chunk.seq ASC, chunk.id ASC LIMIT 1) RETURNING *",
            (
                owner,
                _timestamp(lease_expires_at),
                job_id,
                now,
                self._worker_id,
                self._worker_id,
                now,
            ),
        )
        return None if row is None else _chunk(row)

    async def heartbeat_chunk(self, chunk_id: str, lease_expires_at: datetime) -> None:
        require_transaction(self._connection)
        lease = _timestamp(lease_expires_at)
        now = _timestamp(datetime.now(UTC))
        async with self._connection.execute(
            "UPDATE chunks AS chunk SET lease_expires_at = CASE "
            "WHEN chunk.lease_expires_at IS NULL OR chunk.lease_expires_at < ? THEN ? "
            "ELSE chunk.lease_expires_at END "
            "WHERE chunk.id = ? AND chunk.status = 'inflight' "
            "AND (? IS NULL OR (chunk.lease_owner = ? AND chunk.lease_expires_at > ? "
            "AND EXISTS (SELECT 1 FROM jobs WHERE jobs.id = chunk.job_id "
            "AND jobs.status = 'running' AND jobs.lease_owner = ? "
            "AND jobs.lease_expires_at > ?)))",
            (
                lease,
                lease,
                chunk_id,
                self._worker_id,
                self._worker_id,
                now,
                self._worker_id,
                now,
            ),
        ):
            pass

    async def complete_chunk(self, chunk_id: str) -> None:
        require_transaction(self._connection)
        now = _timestamp(datetime.now(UTC))
        async with self._connection.execute(
            "UPDATE chunks AS chunk SET status = 'done', lease_owner = NULL, "
            "lease_expires_at = NULL WHERE chunk.id = ? AND chunk.status = 'inflight' "
            "AND (? IS NULL OR (chunk.lease_owner = ? AND chunk.lease_expires_at > ? "
            "AND EXISTS (SELECT 1 FROM jobs WHERE jobs.id = chunk.job_id "
            "AND jobs.status = 'running' AND jobs.lease_owner = ? "
            "AND jobs.lease_expires_at > ?)))",
            (
                chunk_id,
                self._worker_id,
                self._worker_id,
                now,
                self._worker_id,
                now,
            ),
        ):
            pass

    async def release_expired_chunks(self, now: datetime) -> list[ChunkRecord]:
        require_transaction(self._connection)
        rows = await _fetch_all(
            self._connection,
            "UPDATE chunks SET status = 'pending', lease_owner = NULL, lease_expires_at = NULL "
            "WHERE status = 'inflight' AND (lease_expires_at IS NULL OR lease_expires_at <= ?) "
            "RETURNING *",
            (_timestamp(now),),
        )
        rows.sort(
            key=lambda row: (
                str(row["job_id"]),
                cast(int, row["seq"]),
                str(row["id"]),
            )
        )
        return [_chunk(row) for row in rows]

    async def record_chunk_attempt(self, attempt: ChunkAttemptRecord) -> None:
        require_transaction(self._connection)
        async with self._connection.execute(
            "INSERT INTO chunk_attempts "
            "(id, chunk_id, attempt_no, tokens_in, tokens_out, cost_usd, latency_ms, outcome, "
            "error_detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                attempt.id,
                attempt.chunk_id,
                attempt.attempt_no,
                attempt.tokens_in,
                attempt.tokens_out,
                attempt.cost_usd,
                attempt.latency_ms,
                attempt.outcome.value,
                attempt.error_detail,
                _timestamp(attempt.created_at),
            ),
        ):
            pass
        async with self._connection.execute(
            "UPDATE jobs SET tokens_in = ("
            "SELECT COALESCE(SUM(attempts.tokens_in), 0) FROM chunk_attempts AS attempts "
            "JOIN chunks ON chunks.id = attempts.chunk_id WHERE chunks.job_id = jobs.id), "
            "tokens_out = ("
            "SELECT COALESCE(SUM(attempts.tokens_out), 0) FROM chunk_attempts AS attempts "
            "JOIN chunks ON chunks.id = attempts.chunk_id WHERE chunks.job_id = jobs.id), "
            "cost_usd = ("
            "SELECT COALESCE(SUM(attempts.cost_usd), 0.0) FROM chunk_attempts AS attempts "
            "JOIN chunks ON chunks.id = attempts.chunk_id WHERE chunks.job_id = jobs.id), "
            "updated_at = ? WHERE id = (SELECT job_id FROM chunks WHERE id = ?)",
            (_timestamp(datetime.now(UTC)), attempt.chunk_id),
        ):
            pass


class SqliteTranslationCacheRepository:
    """Store the first committed translation for each semantic cache key."""

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    async def get_block_translation(
        self,
        translation_key: str,
        block_id: str,
    ) -> str | None:
        row = await _fetch_one(
            self._connection,
            "SELECT translated_text FROM block_translations "
            "WHERE translation_key = ? AND block_id = ?",
            (translation_key, block_id),
        )
        return None if row is None else str(row["translated_text"])

    async def save_block_translation(
        self,
        translation_key: str,
        block_id: str,
        translated_text: str,
    ) -> None:
        require_transaction(self._connection)
        async with self._connection.execute(
            "INSERT OR IGNORE INTO block_translations "
            "(translation_key, block_id, translated_text, created_at) VALUES (?, ?, ?, ?)",
            (
                translation_key,
                block_id,
                translated_text,
                _timestamp(datetime.now(UTC)),
            ),
        ):
            pass
        existing = await self.get_block_translation(translation_key, block_id)
        if existing is None:
            raise RuntimeError("translation insert/read-back returned no row")
