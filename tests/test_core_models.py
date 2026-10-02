"""Tests for strict, roundtrip-safe core models and persistence records."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import BaseModel, ValidationError

from app.core.models import (
    AttemptOutcome,
    Block,
    BlockTranslationRecord,
    ChunkAttemptRecord,
    ChunkBlockRecord,
    ChunkRecord,
    ChunkRequest,
    ChunkResult,
    ChunkStatus,
    DocumentAnalysisRecord,
    DocumentIR,
    DocumentRecord,
    DocumentStatus,
    JobError,
    JobRecord,
    JobStatus,
    TranslationPlan,
    TriageStatus,
)

CREATED_AT = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
BLOCK_DATA = {
    "id": "block-1",
    "seq": 0,
    "source_text": "Hello",
    "source_hash": "sha256:source",
    "format_metadata": {"vendor": {"future": [None, {"field": "value"}]}},
}
PLAN_DATA = {
    "source_language": "en",
    "domain": "general",
    "register": "neutral",
    "terms": ["colour"],
    "warnings": ["regional spelling"],
    "triage_status": TriageStatus.OK,
}


MODEL_FIXTURES: tuple[tuple[type[BaseModel], dict[str, object]], ...] = (
    (Block, BLOCK_DATA),
    (
        DocumentIR,
        {
            "id": "document-1",
            "filename": "input.pdf",
            "format": "pdf",
            "size_bytes": 128,
            "page_count": 1,
            "blocks": [BLOCK_DATA],
        },
    ),
    (TranslationPlan, PLAN_DATA),
    (
        ChunkRequest,
        {
            "chunk_id": "chunk-1",
            "blocks": [BLOCK_DATA],
            "target_language": "de",
            "plan": PLAN_DATA,
            "glossary": {"colour": "Farbe"},
            "model": "test-model",
        },
    ),
    (
        ChunkResult,
        {
            "translations": {"block-1": "Hallo"},
            "tokens_in": 5,
            "tokens_out": 4,
            "model": "test-model",
        },
    ),
    (
        JobError,
        {"error_code": "cost_cap_exceeded", "message": "Cost limit reached", "retryable": False},
    ),
    (
        DocumentRecord,
        {
            "id": "document-1",
            "filename": "input.pdf",
            "format": "pdf",
            "size_bytes": 128,
            "page_count": 1,
            "storage_path": "/data/uploads/document-1",
            "status": DocumentStatus.EXTRACTED,
            "error_code": None,
            "created_at": CREATED_AT,
        },
    ),
    (
        DocumentAnalysisRecord,
        {
            "document_id": "document-1",
            "source_language": "en",
            "domain": "general",
            "register": "neutral",
            "terms": ["colour"],
            "warnings": [],
            "triage_status": TriageStatus.OK,
            "created_at": CREATED_AT,
        },
    ),
    (
        JobRecord,
        {
            "id": "job-1",
            "document_id": "document-1",
            "batch_id": "batch-1",
            "target_language": "de",
            "status": JobStatus.QUEUED,
            "total_chunks": 1,
            "done_chunks": 0,
            "model": "test-model",
            "prompt_version": "v1",
            "glossary": {"colour": "Farbe"},
            "tokens_in": 0,
            "tokens_out": 0,
            "cost_usd": 0.0,
            "error_code": None,
            "error_detail": None,
            "idempotency_key": "request-1",
            "lease_owner": None,
            "lease_expires_at": None,
            "created_at": CREATED_AT,
            "updated_at": CREATED_AT,
        },
    ),
    (
        ChunkRecord,
        {
            "id": "chunk-1",
            "job_id": "job-1",
            "seq": 0,
            "status": ChunkStatus.PENDING,
            "lease_owner": None,
            "lease_expires_at": None,
            "created_at": CREATED_AT,
        },
    ),
    (
        ChunkBlockRecord,
        {"chunk_id": "chunk-1", "block_id": "block-1", "seq_in_chunk": 0},
    ),
    (
        ChunkAttemptRecord,
        {
            "id": "attempt-1",
            "chunk_id": "chunk-1",
            "attempt_no": 1,
            "tokens_in": 5,
            "tokens_out": 4,
            "cost_usd": 0.001,
            "latency_ms": 250,
            "outcome": AttemptOutcome.OK,
            "error_detail": None,
            "created_at": CREATED_AT,
        },
    ),
    (
        BlockTranslationRecord,
        {
            "translation_key": "key-1",
            "block_id": "block-1",
            "translated_text": "Hallo",
            "created_at": CREATED_AT,
        },
    ),
)


@pytest.mark.parametrize("model, data", MODEL_FIXTURES)
def test_models_accept_complete_fixtures_and_roundtrip_json(
    model: type[BaseModel], data: dict[str, object]
) -> None:
    instance = model.model_validate(data)

    assert model.model_validate_json(instance.model_dump_json()) == instance


@pytest.mark.parametrize("model, data", MODEL_FIXTURES)
def test_models_reject_extra_fields(model: type[BaseModel], data: dict[str, object]) -> None:
    with pytest.raises(ValidationError) as exc_info:
        model.model_validate({**data, "unexpected_field": "must be rejected"})

    assert any(error["type"] == "extra_forbidden" for error in exc_info.value.errors())


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("ok", AttemptOutcome.OK),
        ("retryable_error", AttemptOutcome.RETRYABLE_ERROR),
        ("fatal_error", AttemptOutcome.FATAL_ERROR),
    ],
)
def test_chunk_attempt_outcome_accepts_known_values(value: str, expected: AttemptOutcome) -> None:
    data = dict(dict(MODEL_FIXTURES)[ChunkAttemptRecord])
    data["outcome"] = value

    record = ChunkAttemptRecord.model_validate(data)

    assert record.outcome is expected


def test_chunk_attempt_outcome_rejects_unknown_value() -> None:
    data = dict(dict(MODEL_FIXTURES)[ChunkAttemptRecord])
    data["outcome"] = "unknown"

    with pytest.raises(ValidationError):
        ChunkAttemptRecord.model_validate(data)


def test_chunk_record_maps_only_chunks_table_columns() -> None:
    assert "block_ids" not in ChunkRecord.model_fields


def test_document_ir_blocks_default_is_independent_per_instance() -> None:
    first = DocumentIR(id="doc-1", filename="one.pdf", format="pdf", size_bytes=1)
    second = DocumentIR(id="doc-2", filename="two.pdf", format="pdf", size_bytes=1)

    first.blocks.append(Block.model_validate(BLOCK_DATA))

    assert len(first.blocks) == 1
    assert second.blocks == []


def test_job_record_glossary_default_is_independent_per_instance() -> None:
    base_data = dict(dict(MODEL_FIXTURES)[JobRecord])
    base_data.pop("glossary")
    first = JobRecord.model_validate(base_data)
    second = JobRecord.model_validate({**base_data, "id": "job-2"})

    first.glossary["colour"] = "Farbe"

    assert first.glossary == {"colour": "Farbe"}
    assert second.glossary == {}
