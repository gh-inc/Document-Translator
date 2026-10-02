"""Bounded recent-job reads have deterministic creation-time ordering."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.adapters.persistence.api import ApiPersistence
from app.adapters.persistence.database import SqliteConnectionFactory, transaction
from app.adapters.persistence.repositories import (
    SqliteDocumentRepository,
    SqliteJobExecutionRepository,
)
from app.core.models import JobRecord, JobStatus


async def test_recent_jobs_uses_creation_time_and_stable_id_ties(tmp_path: Path) -> None:
    connection = await SqliteConnectionFactory(tmp_path / "recent.db").create()
    try:
        persistence = ApiPersistence(connection)
        assert await persistence.list_recent_jobs(10) == []
        async with transaction(connection):
            await SqliteDocumentRepository(connection).create_document(
                "document", "source.pdf", "pdf", 5, "/uploads/document"
            )
        now = datetime.now(UTC)
        repository = SqliteJobExecutionRepository(connection)
        jobs = []
        for job_id, age in [("old", 2), ("new-b", 0), ("new-a", 0), ("middle", 1)]:
            job = JobRecord(
                id=job_id,
                document_id="document",
                batch_id="batch",
                target_language="de",
                status=JobStatus.QUEUED,
                total_chunks=0,
                done_chunks=0,
                model="test-model",
                prompt_version="v1",
                tokens_in=0,
                tokens_out=0,
                cost_usd=0,
                error_code=None,
                error_detail=None,
                idempotency_key=job_id,
                lease_owner=None,
                lease_expires_at=None,
                created_at=now - timedelta(days=age),
                updated_at=now + timedelta(days=age),
            )
            await repository.create_job_with_chunks(job, [], [])
            jobs.append(job)

        recent = await persistence.list_recent_jobs(3)
        assert [job.id for job in recent] == ["new-a", "new-b", "middle"]
        assert recent == [jobs[2], jobs[1], jobs[3]]
        assert await persistence.list_recent_jobs(1) == [jobs[2]]
        assert await persistence.list_recent_jobs(3) == recent
    finally:
        await connection.close()
