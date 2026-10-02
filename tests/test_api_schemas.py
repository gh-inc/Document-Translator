"""Behavioral roundtrip tests for public REST contracts."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from app.api.schemas import (
    BatchResponse,
    CreateJobRequest,
    DocumentUploadResponse,
    ErrorResponse,
    JobSummaryResponse,
    RetryRequest,
    ServerSentEvent,
)
from app.core.models import DocumentStatus, JobError, JobStatus


def _roundtrip(model: Any, value: dict[str, Any]) -> None:
    instance = model.model_validate(value)
    assert model.model_validate(instance.model_dump()) == instance
    assert model.model_validate_json(instance.model_dump_json()) == instance


def test_create_job_request_requires_at_least_one_target_language() -> None:
    valid = {
        "document_id": "document-1",
        "target_languages": ["de", "fr"],
        "idempotency_key": "request-1",
    }
    _roundtrip(CreateJobRequest, valid)

    with pytest.raises(ValidationError):
        CreateJobRequest.model_validate({**valid, "target_languages": []})


def test_document_upload_response_roundtrips_domain_status() -> None:
    _roundtrip(
        DocumentUploadResponse,
        {
            "id": "document-1",
            "filename": "report.pdf",
            "format": "pdf",
            "status": DocumentStatus.EXTRACTED,
            "block_count": 12,
        },
    )


def test_batch_and_job_summary_responses_roundtrip_nested_error() -> None:
    error = JobError(
        error_code="cost_cap_exceeded",
        message="Translation provider is unavailable",
        retryable=True,
    )
    job = JobSummaryResponse(
        id="job-1",
        document_id="document-1",
        batch_id="batch-1",
        target_language="de",
        status=JobStatus.COMPLETED_WITH_ERRORS,
        total_chunks=4,
        done_chunks=3,
        cost_usd=0.025,
        error=error,
    )
    batch = BatchResponse(batch_id="batch-1", jobs=[job])

    _roundtrip(JobSummaryResponse, job.model_dump())
    _roundtrip(BatchResponse, batch.model_dump())
    assert batch.jobs[0].error == error


def test_retry_request_default_and_explicit_cost_cap_roundtrip() -> None:
    _roundtrip(RetryRequest, {})
    _roundtrip(RetryRequest, {"raised_cost_cap_usd": 1.25})


def test_server_sent_event_and_error_response_roundtrip() -> None:
    error = {
        "error_code": "scanned_pdf",
        "message": "A chunk could not be translated",
        "retryable": False,
    }
    event = {
        "job_id": "job-1",
        "event": "error",
        "status": JobStatus.FAILED,
        "done_chunks": 2,
        "total_chunks": 3,
        "cost_usd": 0.01,
        "error": error,
    }

    _roundtrip(ServerSentEvent, event)
    _roundtrip(ErrorResponse, error)
