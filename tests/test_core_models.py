"""Tests for strict, roundtrip-safe core models and persistence records."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

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
    RenderResult,
    TranslationPlan,
    TriageAgentOutput,
    TriageResult,
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
        RenderResult,
        {
            "output_path": Path("/tmp/output.pdf"),
            "degraded_block_ids": ["block-1"],
            "fallback_blocks": 2,
            "fallback_pages": 3,
        },
    ),
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
        TriageResult,
        {
            "plan": PLAN_DATA,
            "model": "gpt-4o-mini",
            "tokens_in": 120,
            "tokens_out": 30,
            "cached_tokens_in": 80,
            "requests": 2,
        },
    ),
    (TriageAgentOutput, {"reasoning": "English text in sampled sections", "plan": PLAN_DATA}),
    (
        ChunkRequest,
        {
            "chunk_id": "chunk-1",
            "blocks": [BLOCK_DATA],
            "target_language": "de",
            "plan": PLAN_DATA,
            "glossary": {"colour": "Farbe"},
            "model": "test-model",
            "context_before": [{**BLOCK_DATA, "id": "before"}],
            "context_after": [{**BLOCK_DATA, "id": "after"}],
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
    first.warnings.append("U+1F3DB")
    assert second.warnings == []


def test_render_result_degradation_defaults_are_independent() -> None:
    first = RenderResult(output_path=Path("one.pdf"))
    second = RenderResult(output_path=Path("two.pdf"))
    first.degraded_block_ids.append("block-1")
    assert second.degraded_block_ids == []
    assert second.fallback_blocks == second.fallback_pages == 0


def test_job_record_glossary_default_is_independent_per_instance() -> None:
    base_data = dict(dict(MODEL_FIXTURES)[JobRecord])
    base_data.pop("glossary")
    first = JobRecord.model_validate(base_data)
    second = JobRecord.model_validate({**base_data, "id": "job-2"})

    first.glossary["colour"] = "Farbe"

    assert first.glossary == {"colour": "Farbe"}
    assert second.glossary == {}


def test_chunk_request_roundtrips_context_blocks() -> None:
    data = dict(dict(MODEL_FIXTURES)[ChunkRequest])
    data["context_before"] = [{**BLOCK_DATA, "id": "before"}]
    data["context_after"] = [{**BLOCK_DATA, "id": "after"}]
    instance = ChunkRequest.model_validate(data)
    assert ChunkRequest.model_validate_json(instance.model_dump_json()) == instance
    assert instance.context_before[0].id == "before"
    assert instance.context_after[0].id == "after"


def test_chunk_request_context_defaults_are_independent() -> None:
    data = dict(dict(MODEL_FIXTURES)[ChunkRequest])
    data.pop("context_before", None)
    data.pop("context_after", None)
    first = ChunkRequest.model_validate(data)
    second = ChunkRequest.model_validate(data)
    first.context_before.append(Block.model_validate(BLOCK_DATA))
    first.context_after.append(Block.model_validate(BLOCK_DATA))
    assert second.context_before == []
    assert second.context_after == []


def test_analyzing_status_roundtrips_json() -> None:
    data = dict(dict(MODEL_FIXTURES)[DocumentRecord])
    data["status"] = DocumentStatus.ANALYZING
    document = DocumentRecord.model_validate(data)
    assert '"status":"analyzing"' in document.model_dump_json()
    assert (
        DocumentRecord.model_validate_json(document.model_dump_json()).status
        is DocumentStatus.ANALYZING
    )


def test_triage_usage_defaults_and_negative_counts() -> None:
    result = TriageResult(plan=TranslationPlan.model_validate(PLAN_DATA), model="gpt-4o-mini")
    assert result.tokens_in == result.tokens_out == result.cached_tokens_in == result.requests == 0
    for field in ("tokens_in", "tokens_out", "cached_tokens_in", "requests"):
        with pytest.raises(ValidationError):
            TriageResult.model_validate({"plan": PLAN_DATA, "model": "gpt-4o-mini", field: -1})
