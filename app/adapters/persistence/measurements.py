"""Read-only persisted usage snapshots for the explicit measurement command."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from aiosqlite import Connection

from app.adapters.persistence.database import require_connection_access


class MeasurementPersistence:
    """Load job and attempt measurements without exposing SQL to the CLI."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    async def get_job_measurement(self, job_id: str) -> dict[str, object] | None:
        require_connection_access(self._connection)
        async with self._connection.execute(
            "SELECT id, document_id, target_language, status, model, total_chunks, done_chunks, "
            "tokens_in, tokens_out, cost_usd, created_at, updated_at "
            "FROM jobs WHERE id = ?",
            (job_id,),
        ) as cursor:
            job = await cursor.fetchone()
        if job is None:
            return None

        async with self._connection.execute(
            "SELECT chunk_attempts.attempt_no, chunk_attempts.tokens_in, "
            "chunk_attempts.tokens_out, chunk_attempts.cost_usd, "
            "chunk_attempts.latency_ms, chunk_attempts.outcome "
            "FROM chunk_attempts JOIN chunks ON chunks.id = chunk_attempts.chunk_id "
            "WHERE chunks.job_id = ? ORDER BY chunks.seq, chunk_attempts.attempt_no",
            (job_id,),
        ) as cursor:
            attempts = list(await cursor.fetchall())

        async with self._connection.execute(
            "SELECT COUNT(DISTINCT chunk_blocks.block_id) FROM chunk_blocks "
            "JOIN chunks ON chunks.id = chunk_blocks.chunk_id WHERE chunks.job_id = ?",
            (job_id,),
        ) as cursor:
            block_row = await cursor.fetchone()

        attempt_cost = sum(float(attempt["cost_usd"]) for attempt in attempts)
        retry_cost = sum(
            float(attempt["cost_usd"]) for attempt in attempts if int(attempt["attempt_no"]) > 1
        )
        latencies = [int(attempt["latency_ms"]) for attempt in attempts]
        created_at = _parse_datetime(str(job["created_at"]))
        updated_at = _parse_datetime(str(job["updated_at"]))
        return {
            "job_id": str(job["id"]),
            "document_id": str(job["document_id"]),
            "target_language": str(job["target_language"]),
            "status": str(job["status"]),
            "model": str(job["model"]),
            "total_chunks": int(job["total_chunks"]),
            "done_chunks": int(job["done_chunks"]),
            "block_count": int(block_row[0]) if block_row is not None else 0,
            "tokens_in": int(job["tokens_in"]),
            "tokens_out": int(job["tokens_out"]),
            "cost_usd": float(job["cost_usd"]),
            "created_at": created_at.isoformat(),
            "updated_at": updated_at.isoformat(),
            "job_latency_ms": max(0, round((updated_at - created_at).total_seconds() * 1000)),
            "attempt_count": len(attempts),
            "retry_attempt_count": sum(int(attempt["attempt_no"]) > 1 for attempt in attempts),
            "retry_cost_usd": retry_cost,
            "attempt_cost_usd": attempt_cost,
            "retry_cost_share_percent": 100.0 * retry_cost / attempt_cost if attempt_cost else 0.0,
            "attempt_tokens_in": sum(int(attempt["tokens_in"]) for attempt in attempts),
            "attempt_tokens_out": sum(int(attempt["tokens_out"]) for attempt in attempts),
            "attempt_outcomes": _count_outcomes(attempts),
            "chunk_attempt_latencies_ms": latencies,
            "p95_chunk_attempt_latency_ms": _percentile_nearest_rank(latencies, 95),
        }


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed


def _count_outcomes(attempts: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for attempt in attempts:
        outcome = str(attempt["outcome"])
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts


def _percentile_nearest_rank(values: list[int], percentile: int) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered) / 100) - 1)
    return ordered[index]
