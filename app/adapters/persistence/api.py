"""Persistence helpers used by the REST application services.

This module keeps API-specific SQL inside the persistence adapter. It does not
add or change a public repository port; it coordinates API reads, metrics, and
the retry transition using the existing schema.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from aiosqlite import Connection

from app.adapters.persistence.database import (
    require_connection_access,
    require_transaction,
    transaction,
)
from app.adapters.persistence.repositories import SqliteJobExecutionRepository
from app.core.errors import AnalysisPendingError
from app.core.models import JobRecord, JobStatus


class ApiPersistenceConflict(RuntimeError):
    """A job cannot undergo the requested API state transition."""


class ApiPersistenceInvalid(ValueError):
    """An API persistence operation received an invalid value."""


class ApiJobExecutionRepository(SqliteJobExecutionRepository):
    """Serialize request-key family validation with each aggregate insert."""

    async def _validate_new_job(self, job: JobRecord) -> None:
        async with self._connection.execute(
            "SELECT documents.status, document_analyses.terms FROM documents "
            "LEFT JOIN document_analyses ON document_analyses.document_id = documents.id "
            "WHERE documents.id = ?",
            (job.document_id,),
        ) as cursor:
            document = await cursor.fetchone()
        if document is not None and (
            document["status"] != "extracted" or document["terms"] is None
        ):
            raise AnalysisPendingError("document analysis is pending")
        if document is not None:
            terms = json.loads(document["terms"])
            # Chunk grouping yields before this transaction. A completed triage
            # retry may have replaced the terms used to build the job glossary.
            # Other plan fields are read by the worker after this insert; the
            # first job freezes them. Reject stale glossary snapshots here.
            if {term: term for term in terms} != job.glossary:
                raise AnalysisPendingError("document analysis changed during planning")
        request_digest = job.batch_id.partition(".")[0]
        async with self._connection.execute(
            "SELECT document_id, batch_id FROM jobs WHERE batch_id LIKE ? LIMIT 1",
            (f"{request_digest}.%",),
        ) as cursor:
            row = await cursor.fetchone()
        if row is not None and (
            row["document_id"] != job.document_id or row["batch_id"] != job.batch_id
        ):
            raise ApiPersistenceConflict("idempotency key was used for another request")


class ApiPersistence:
    """Internal query and transaction support for API services."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection
        self._lock = asyncio.Lock()
        self._jobs = ApiJobExecutionRepository(connection)

    @asynccontextmanager
    async def read(self) -> AsyncIterator[None]:
        async with self._lock:
            require_connection_access(self._connection)
            yield

    @asynccontextmanager
    async def write(self) -> AsyncIterator[None]:
        async with self._lock, transaction(self._connection):
            yield

    async def find_by_idempotency_key(self, key: str) -> JobRecord | None:
        require_connection_access(self._connection)
        async with self._connection.execute(
            "SELECT id FROM jobs WHERE idempotency_key = ?",
            (key,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return await self._jobs.get_job(str(row["id"]))

    async def get_job(self, job_id: str) -> JobRecord | None:
        return await self._jobs.get_job(job_id)

    async def get_jobs_by_batch(self, batch_id: str) -> list[JobRecord]:
        return await self._jobs.get_jobs_by_batch(batch_id)

    async def list_recent_jobs(self, limit: int) -> list[JobRecord]:
        """Read newest jobs, using their ids to resolve creation-time ties."""
        require_connection_access(self._connection)
        async with self._connection.execute(
            "SELECT id FROM jobs ORDER BY created_at DESC, id ASC LIMIT ?",
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
        jobs: list[JobRecord] = []
        for row in rows:
            job = await self._jobs.get_job(str(row["id"]))
            if job is not None:
                jobs.append(job)
        return jobs

    async def get_batch_family(self, request_digest: str) -> list[JobRecord]:
        """Find jobs sharing the stable request-key prefix in their batch id."""
        require_connection_access(self._connection)
        async with self._connection.execute(
            "SELECT id FROM jobs WHERE batch_id LIKE ? ORDER BY created_at ASC, id ASC",
            (f"{request_digest}.%",),
        ) as cursor:
            rows = await cursor.fetchall()
        jobs: list[JobRecord] = []
        for row in rows:
            job = await self._jobs.get_job(str(row["id"]))
            if job is not None:
                jobs.append(job)
        return jobs

    async def health_check(self) -> bool:
        require_connection_access(self._connection)
        async with self._connection.execute("SELECT 1") as cursor:
            row = await cursor.fetchone()
        return row is not None and int(row[0]) == 1

    async def stale_inflight_chunk_count(self, stale_before: datetime) -> int:
        """Count inflight chunks whose non-null lease expired before the cutoff."""
        require_connection_access(self._connection)
        async with self._connection.execute(
            "SELECT COUNT(*) FROM chunks WHERE status = 'inflight' "
            "AND lease_expires_at IS NOT NULL AND lease_expires_at < ?",
            (stale_before.astimezone(UTC).isoformat(timespec="microseconds"),),
        ) as cursor:
            row = await cursor.fetchone()
        return 0 if row is None else int(row[0])

    async def metrics_snapshot(self) -> dict[str, object]:
        """Return persisted job and attempt aggregates for Prometheus export."""
        require_connection_access(self._connection)
        async with self._connection.execute(
            "SELECT status, COUNT(*) AS count FROM jobs GROUP BY status"
        ) as cursor:
            status_rows = await cursor.fetchall()
        async with self._connection.execute(
            "SELECT COALESCE(SUM(cost_usd), 0.0) FROM jobs"
        ) as cursor:
            cost_row = await cursor.fetchone()
        async with self._connection.execute(
            "SELECT COUNT(*) FROM chunk_attempts WHERE outcome != 'ok'"
        ) as cursor:
            errors_row = await cursor.fetchone()
        return {
            "jobs_by_status": {str(row["status"]): int(row["count"]) for row in status_rows},
            "llm_cost_usd_total": 0.0 if cost_row is None else float(cost_row[0]),
            "llm_errors_total": 0 if errors_row is None else int(errors_row[0]),
            # Cache hits are intentionally not persisted by the approved schema.
            "cache_hits_total": 0,
        }

    async def retry_job(
        self,
        job_id: str,
        *,
        translation_key: str,
        raised_cost_cap_usd: float | None,
        default_cost_cap_usd: float,
        default_max_attempts: int,
    ) -> JobRecord | None:
        """Requeue only uncached chunks of a terminal job in one transaction.

        Attempt rows and their billed usage remain untouched. The JSON policy
        stored in ``error_detail`` is internal worker state while ``error_code``
        is null, allowing raised budgets and per-chunk attempt baselines to
        survive an API or worker restart without changing the public schema.
        """
        cap = default_cost_cap_usd if raised_cost_cap_usd is None else raised_cost_cap_usd
        if (
            isinstance(cap, bool)
            or not isinstance(cap, int | float)
            or not math.isfinite(cap)
            or cap <= 0
        ):
            raise ApiPersistenceInvalid("retry cost cap must be a finite positive number")

        require_transaction(self._connection)
        now = datetime.now(UTC).isoformat(timespec="microseconds")
        async with self._connection.execute(
            "SELECT * FROM jobs WHERE id = ?",
            (job_id,),
        ) as cursor:
            job_row = await cursor.fetchone()
        if job_row is None:
            return None

        job = await self._jobs.get_job(job_id)
        if job is None:
            return None
        if job.status not in {JobStatus.FAILED, JobStatus.COMPLETED_WITH_ERRORS}:
            raise ApiPersistenceConflict("job is not in a retryable terminal state")
        if job.lease_expires_at is not None and job.lease_expires_at > datetime.now(UTC):
            raise ApiPersistenceConflict("job still has an active lease")

        if raised_cost_cap_usd is not None and (cap <= default_cost_cap_usd or cap <= job.cost_usd):
            raise ApiPersistenceInvalid(
                "raised cost cap must exceed both the default cap and current cost"
            )

        async with self._connection.execute(
            "SELECT 1 FROM chunks WHERE job_id = ? AND status = 'inflight' "
            "AND lease_expires_at > ? LIMIT 1",
            (job_id, now),
        ) as cursor:
            active_chunk = await cursor.fetchone()
        if active_chunk is not None:
            raise ApiPersistenceConflict("job still has an active chunk lease")

        async with self._connection.execute(
            "SELECT chunks.id, chunks.status, chunks.seq, "
            "COALESCE(MAX(chunk_attempts.attempt_no), 0) AS max_attempt_no "
            "FROM chunks LEFT JOIN chunk_attempts ON chunk_attempts.chunk_id = chunks.id "
            "WHERE chunks.job_id = ? GROUP BY chunks.id ORDER BY chunks.seq ASC",
            (job_id,),
        ) as cursor:
            chunks = await cursor.fetchall()

        attempt_baselines: dict[str, int] = {}
        done_count = 0
        for chunk in chunks:
            chunk_id = str(chunk["id"])
            async with self._connection.execute(
                "SELECT links.block_id FROM chunk_blocks AS links "
                "LEFT JOIN block_translations AS translations "
                "ON translations.translation_key = ? AND translations.block_id = links.block_id "
                "WHERE links.chunk_id = ? AND translations.block_id IS NULL",
                (translation_key, chunk_id),
            ) as cursor:
                missing = await cursor.fetchone()

            if missing is None:
                async with self._connection.execute(
                    "UPDATE chunks SET status = 'done', lease_owner = NULL, "
                    "lease_expires_at = NULL WHERE id = ?",
                    (chunk_id,),
                ):
                    pass
                done_count += 1
            else:
                async with self._connection.execute(
                    "UPDATE chunks SET status = 'pending', lease_owner = NULL, "
                    "lease_expires_at = NULL WHERE id = ?",
                    (chunk_id,),
                ):
                    pass
                attempt_baselines[chunk_id] = int(chunk["max_attempt_no"])

        policy = {
            "_internal_retry_policy": {
                "version": 1,
                "max_cost_per_job_usd": float(cap),
                "max_chunk_attempts": int(default_max_attempts),
                "attempt_baselines": attempt_baselines,
            }
        }
        async with self._connection.execute(
            "UPDATE jobs SET status = 'queued', done_chunks = ?, error_code = NULL, "
            "error_detail = ?, lease_owner = NULL, lease_expires_at = NULL, updated_at = ? "
            "WHERE id = ?",
            (done_count, json.dumps(policy, separators=(",", ":")), now, job_id),
        ):
            pass
        return await self._jobs.get_job(job_id)
