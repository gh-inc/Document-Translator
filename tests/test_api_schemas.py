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


def test_document_upload_response_roundtrips_warnings_and_defaults_to_empty() -> None:
    value = {
        "id": "document-1",
        "filename": "report.pdf",
        "format": "pdf",
        "status": DocumentStatus.ANALYZING,
        "block_count": 12,
    }
    first = DocumentUploadResponse.model_validate(value)
    second = DocumentUploadResponse.model_validate(value)
    assert first.warnings == []
    first.warnings.append("Unsupported source character: U+1F3DB")
    assert second.warnings == []
    _roundtrip(DocumentUploadResponse, first.model_dump())


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
        cache_hit_blocks=12,
        cache_miss_blocks=3,
        cost_usd=0.025,
        error=error,
    )
    batch = BatchResponse(batch_id="batch-1", jobs=[job])

    _roundtrip(JobSummaryResponse, job.model_dump())
    _roundtrip(BatchResponse, batch.model_dump())
    assert batch.jobs[0].error == error
    assert (batch.jobs[0].cache_hit_blocks, batch.jobs[0].cache_miss_blocks) == (12, 3)


def test_retry_request_default_and_explicit_cost_cap_roundtrip() -> None:
    _roundtrip(RetryRequest, {})
    _roundtrip(RetryRequest, {"raised_cost_cap_usd": 1.25})


@pytest.mark.parametrize("field", ["cache_hit_blocks", "cache_miss_blocks"])
def test_cache_counts_default_to_zero_and_reject_negative_values(field: str) -> None:
    job = JobSummaryResponse(
        id="job-1",
        document_id="document-1",
        batch_id="batch-1",
        target_language="de",
        status=JobStatus.QUEUED,
        total_chunks=1,
        done_chunks=0,
        cost_usd=0,
        error=None,
    )
    assert (job.cache_hit_blocks, job.cache_miss_blocks) == (0, 0)
    with pytest.raises(ValidationError):
        JobSummaryResponse.model_validate({**job.model_dump(), field: -1})


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
        "cache_hit_blocks": 2,
        "cache_miss_blocks": 1,
        "cost_usd": 0.01,
        "error": error,
    }

    _roundtrip(ServerSentEvent, event)
    _roundtrip(ErrorResponse, error)
