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


class ChunkBlockRecord(BaseModel):
    """Persistence record for the chunk_blocks join table."""

    model_config = ConfigDict(extra="forbid")

    chunk_id: str
    block_id: str
    seq_in_chunk: int
