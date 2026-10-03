"""Core domain and persistence record models."""

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DocumentStatus(StrEnum):
    UPLOADED = "uploaded"
    ANALYZING = "analyzing"
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
    warnings: list[str] = Field(default_factory=list)


class RenderResult(BaseModel):
    """Outcome of rendering one translated document."""

    model_config = ConfigDict(extra="forbid")

    output_path: Path
    degraded_block_ids: list[str] = Field(default_factory=list)
    fallback_blocks: int = 0
    fallback_pages: int = 0


class TranslationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_language: str
    domain: str
    register: str
    terms: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    triage_status: TriageStatus = TriageStatus.OK


class TriageResult(BaseModel):
    """A triage plan plus the provider usage that produced it."""

    model_config = ConfigDict(extra="forbid")

    plan: TranslationPlan
    model: str
    tokens_in: int = Field(default=0, ge=0)
    tokens_out: int = Field(default=0, ge=0)
    cached_tokens_in: int = Field(default=0, ge=0)
    requests: int = Field(default=0, ge=0)


class TriageAgentOutput(BaseModel):
    """Structured triage result with a brief evidence-based explanation."""

    model_config = ConfigDict(extra="forbid")

    reasoning: str
    plan: TranslationPlan


class ChunkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunk_id: str
    blocks: list[Block]
    target_language: str
    plan: TranslationPlan
    glossary: dict[str, str] = Field(default_factory=dict)
    model: str
    context_before: list[Block] = Field(default_factory=list)
    context_after: list[Block] = Field(default_factory=list)


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
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    cost_usd_total: float = 0.0
    tokens_in_total: int = 0
    tokens_out_total: int = 0


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
