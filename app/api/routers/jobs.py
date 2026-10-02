"""Thin REST endpoints for job creation, status, retry, and results."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app.api.dependencies import get_file_storage, get_job_service
from app.api.responses import DownloadResponse
from app.api.schemas import (
    BatchResponse,
    CreateJobRequest,
    JobSummaryResponse,
    RetryRequest,
    ServerSentEvent,
)
from app.core.errors import ErrorCode, ServiceError, catalog_entry
from app.core.models import JobError, JobRecord, JobStatus
from app.core.ports import FileStorage
from app.core.services.job_service import JobService

router = APIRouter()
_TERMINAL_STATUSES = {
    JobStatus.DONE,
    JobStatus.COMPLETED_WITH_ERRORS,
    JobStatus.FAILED,
}


@router.post("/api/jobs", response_model=BatchResponse)
async def create_jobs(
    request: CreateJobRequest,
    service: Annotated[JobService, Depends(get_job_service)],
) -> BatchResponse:
    jobs = await service.create_jobs(
        request.document_id,
        request.target_languages,
        request.idempotency_key,
    )
    if not jobs:
        raise ServiceError(ErrorCode.INTERNAL_ERROR, status_code=500)
    return BatchResponse(
        batch_id=jobs[0].batch_id,
        jobs=[_summary(job) for job in jobs],
    )


@router.get("/api/jobs", response_model=list[JobSummaryResponse])
async def list_recent_jobs(
    service: Annotated[JobService, Depends(get_job_service)],
    limit: int = 10,
) -> list[JobSummaryResponse]:
    return [_summary(job) for job in await service.list_recent_jobs(limit)]


@router.get("/api/jobs/{job_id}", response_model=JobSummaryResponse)
async def get_job(
    job_id: str,
    service: Annotated[JobService, Depends(get_job_service)],
) -> JobSummaryResponse:
    job = await service.get_job(job_id)
    if job is None:
        raise ServiceError(ErrorCode.NOT_FOUND, status_code=404)
    return _summary(job)


@router.post("/api/jobs/{job_id}/retry", response_model=JobSummaryResponse)
async def retry_job(
    job_id: str,
    request: RetryRequest,
    service: Annotated[JobService, Depends(get_job_service)],
) -> JobSummaryResponse:
    job = await service.retry_job(job_id, request.raised_cost_cap_usd)
    if job is None:
        raise ServiceError(ErrorCode.NOT_FOUND, status_code=404)
    return _summary(job)


@router.get("/api/jobs/{job_id}/events")
async def job_events(
    job_id: str,
    request: Request,
    service: Annotated[JobService, Depends(get_job_service)],
) -> StreamingResponse:
    if await service.get_job(job_id) is None:
        raise ServiceError(ErrorCode.NOT_FOUND, status_code=404)
    return StreamingResponse(
        event_stream(request, service, job_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def event_stream(
    request: Request,
    service: JobService,
    job_id: str,
) -> AsyncIterator[bytes]:
    """Poll a job until it is terminal or its client disconnects."""
    previous: JobRecord | None = None
    while True:
        if await request.is_disconnected():
            break

        job = await service.get_job(job_id)
        if job is None:
            break

        if job.status is JobStatus.FAILED:
            event = "error"
        elif job.status in _TERMINAL_STATUSES:
            event = "done"
        elif previous is None or job.status != previous.status:
            event = "status"
        else:
            event = "progress"

        payload = ServerSentEvent(
            job_id=job.id,
            event=event,
            status=job.status,
            done_chunks=job.done_chunks,
            total_chunks=job.total_chunks,
            cost_usd=job.cost_usd,
            error=_safe_job_error(job.error_code),
        )
        yield f"event: {event}\ndata: {payload.model_dump_json()}\n\n".encode()

        if job.status in _TERMINAL_STATUSES:
            break
        previous = job
        await asyncio.sleep(1)


@router.get("/api/jobs/{job_id}/download")
async def download_job(
    job_id: str,
    service: Annotated[JobService, Depends(get_job_service)],
    storage: Annotated[FileStorage, Depends(get_file_storage)],
) -> DownloadResponse:
    job = await service.get_job(job_id)
    if job is None:
        raise ServiceError(ErrorCode.NOT_FOUND, status_code=404)
    if job.status not in {JobStatus.DONE, JobStatus.COMPLETED_WITH_ERRORS}:
        raise ServiceError(ErrorCode.CONFLICT, status_code=409)
    try:
        output_path = await storage.get_output_path(job.id)
    except FileNotFoundError as error:
        # A terminal job without an artifact is an internal state inconsistency;
        # never expose filesystem details to the caller.
        raise ServiceError(ErrorCode.INTERNAL_ERROR, status_code=500) from error
    return DownloadResponse(output_path, filename=output_path.name)


@router.get("/api/batches/{batch_id}", response_model=BatchResponse)
async def get_batch(
    batch_id: str,
    service: Annotated[JobService, Depends(get_job_service)],
) -> BatchResponse:
    jobs = await service.get_jobs_by_batch(batch_id)
    if not jobs:
        raise ServiceError(ErrorCode.NOT_FOUND, status_code=404)
    return BatchResponse(batch_id=batch_id, jobs=[_summary(job) for job in jobs])


def _summary(job: JobRecord) -> JobSummaryResponse:
    return JobSummaryResponse(
        id=job.id,
        document_id=job.document_id,
        batch_id=job.batch_id,
        target_language=job.target_language,
        status=job.status,
        total_chunks=job.total_chunks,
        done_chunks=job.done_chunks,
        cost_usd=job.cost_usd,
        error=_safe_job_error(job.error_code),
    )


def _safe_job_error(error_code: str | None) -> JobError | None:
    if error_code is None:
        return None
    try:
        safe_code = ErrorCode(error_code)
    except ValueError:
        safe_code = ErrorCode.INTERNAL_ERROR
    message, retryable = catalog_entry(safe_code.value)
    return JobError(error_code=safe_code.value, message=message, retryable=retryable)
