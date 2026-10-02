# Core Models Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Finalize `app/core/models.py` as a clean Pydantic module where domain and persistence record models are strict, roundtrip-safe, and adhere to a 1:1 record-to-table-column mapping.

**Architecture:** All models derive from `BaseModel` with `ConfigDict(extra="forbid")`. Records mirror SQLite table columns exactly; no join data leaks into table records. `format_metadata` remains an opaque JSON blob at the `Block` level only.

**Tech Stack:** Python 3.12, Pydantic v2.

**Status:** Implemented and validated under DT-7. The combined suite passes
79 tests; `make lint` and `make typecheck` are clean. The approved `register`
fields retain Pydantic's BaseModel-shadowing warnings; serialized names are unchanged.

---

## Task 1: Add Missing Record Models

**Files:**
- Modify: `app/core/models.py`
- Modify: `tests/test_domain_contracts.py`

Add:
- `AttemptOutcome` enum for `chunk_attempts.outcome`.
- `DocumentAnalysisRecord` for the `document_analyses` table.
- `ChunkBlockRecord` for the `chunk_blocks` join table.

Remove any temptation to add `block_ids` to `ChunkRecord`; it must stay 1:1 with the `chunks` table.

**Step 1: Write the failing contract test**

```python
def test_chunk_record_has_no_block_ids_field() -> None:
    fields = set(ChunkRecord.model_fields.keys())
    assert "block_ids" not in fields, "ChunkRecord must map 1:1 to the chunks table"
```

Run: `pytest tests/test_domain_contracts.py::test_chunk_record_has_no_block_ids_field -v`
Expected: PASS (test is defensive; current code already lacks the field).

**Step 2: Add the new models**

Add `AttemptOutcome`, `DocumentAnalysisRecord`, and `ChunkBlockRecord` to `app/core/models.py` as shown in the final code below.

**Step 3: Run tests**

Run: `pytest tests/test_domain_contracts.py -v`
Expected: PASS

---

## Task 2: Apply `ConfigDict(extra="forbid")` Globally

**Files:**
- Modify: `app/core/models.py`

Add `model_config = ConfigDict(extra="forbid")` to every `BaseModel` subclass to reject unexpected fields early.

**Step 1: Write the failing test**

```python
def test_record_models_reject_extra_fields() -> None:
    with pytest.raises(ValidationError) as exc_info:
        JobRecord(
            id="job-1",
            document_id="doc-1",
            batch_id="batch-1",
            target_language="de",
            status=JobStatus.QUEUED,
            total_chunks=0,
            done_chunks=0,
            model="test-model",
            prompt_version="v1",
            glossary={},
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            error_code=None,
            error_detail=None,
            idempotency_key="key-1",
            lease_owner=None,
            lease_expires_at=None,
            created_at=datetime.now(),
            updated_at=datetime.now(),
            unexpected_field="should fail",
        )
    assert any(error["type"] == "extra_forbidden" for error in exc_info.value.errors())
```

Run: `pytest tests/test_domain_contracts.py::test_record_models_reject_extra_fields -v`
Expected: FAIL before implementation, PASS after.

**Step 2: Add `ConfigDict(extra="forbid")` to all models**

See final code below.

**Step 3: Run tests**

Run: `pytest tests/test_domain_contracts.py -v`
Expected: PASS

---

## Task 3: Preserve Opaque Metadata Roundtrip

**Files:**
- Modify: `tests/test_domain_contracts.py` (if needed)

Ensure existing tests still prove that arbitrary JSON shapes in `Block.format_metadata` survive `model_dump_json()` / `model_validate_json()` through nested core models (`DocumentIR`, `ChunkRequest`).

Run: `pytest tests/test_domain_contracts.py -v`
Expected: PASS

---

## Task 4: Run Full Verification

```bash
make test
make lint
make typecheck
```

Expected: all green.

---

## Task 5: Commit

```bash
git add app/core/models.py tests/test_domain_contracts.py
git commit -m "DT-7: feat(core): finalize strict Pydantic domain and persistence models"
```

---

## Final Code for `app/core/models.py`

**Execution notes:** DT-7 implements these model semantics. Strict-model tests
live in `tests/test_core_models.py` so DT-8 can update the existing port tests
without concurrent edits to the same file. The draft extra-field test was
corrected to supply all required nullable fields and assert `extra_forbidden`,
avoiding false positives from missing-field errors. The referenced
`superpowers:executing-plans` skill is unavailable; the main agent coordinates
the implementation with the available collaboration tools.

```python
"""Core domain and persistence record models."""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DocumentStatus(StrEnum):
    UPLOADED = "uploaded"
    EXTRACTED = "extracted"
    FAILED = "failed"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    ASSEMBLING = "assembling"
    DONE = "done"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    FAILED = "failed"


class ChunkStatus(StrEnum):
    PENDING = "pending"
    INFLIGHT = "inflight"
    DONE = "done"


class TriageStatus(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"


class AttemptOutcome(StrEnum):
    OK = "ok"
    RETRYABLE_ERROR = "retryable_error"
    FATAL_ERROR = "fatal_error"


class Block(BaseModel):
    """Document unit whose format metadata remains opaque to core services."""

    model_config = ConfigDict(extra="forbid")

    id: str
    seq: int
    source_text: str
    source_hash: str
    format_metadata: dict[str, Any] = Field(default_factory=dict)


class DocumentIR(BaseModel):
    """In-memory document representation produced by an extractor."""

    model_config = ConfigDict(extra="forbid")

    id: str
    filename: str
    format: str
    size_bytes: int
    page_count: int | None = None
    blocks: list[Block] = Field(default_factory=list)


class TranslationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_language: str
    domain: str
    register: str
    terms: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    triage_status: TriageStatus = TriageStatus.OK


class ChunkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunk_id: str
    blocks: list[Block]
    target_language: str
    plan: TranslationPlan
    glossary: dict[str, str] = Field(default_factory=dict)
    model: str


class ChunkResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    translations: dict[str, str]
    tokens_in: int
    tokens_out: int
    model: str


class JobError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error_code: str
    message: str
    retryable: bool


class DocumentRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    filename: str
    format: str
    size_bytes: int
    page_count: int | None
    storage_path: str
    status: DocumentStatus
    error_code: str | None
    created_at: datetime


class DocumentAnalysisRecord(BaseModel):
    """Persistence record for the document_analyses table."""

    model_config = ConfigDict(extra="forbid")

    document_id: str
    source_language: str
    domain: str
    register: str
    terms: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    triage_status: TriageStatus = TriageStatus.OK
    created_at: datetime


class JobRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    document_id: str
    batch_id: str
    target_language: str
    status: JobStatus
    total_chunks: int
    done_chunks: int
    model: str
    prompt_version: str
    glossary: dict[str, str] = Field(default_factory=dict)
    tokens_in: int
    tokens_out: int
    cost_usd: float
    error_code: str | None
    error_detail: str | None
    idempotency_key: str
    lease_owner: str | None
    lease_expires_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ChunkRecord(BaseModel):
    """Persistence record for the chunks table — 1:1 with table columns."""

    model_config = ConfigDict(extra="forbid")

    id: str
    job_id: str
    seq: int
    status: ChunkStatus
    lease_owner: str | None
    lease_expires_at: datetime | None
    created_at: datetime


class ChunkBlockRecord(BaseModel):
    """Persistence record for the chunk_blocks join table."""

    model_config = ConfigDict(extra="forbid")

    chunk_id: str
    block_id: str
    seq_in_chunk: int


class ChunkAttemptRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    chunk_id: str
    attempt_no: int
    tokens_in: int
    tokens_out: int
    cost_usd: float
    latency_ms: int
    outcome: AttemptOutcome
    error_detail: str | None
    created_at: datetime


class BlockTranslationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    translation_key: str
    block_id: str
    translated_text: str
    created_at: datetime
```
