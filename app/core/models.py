"""Core domain and persistence record models."""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


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


class Block(BaseModel):
    """Document unit whose format metadata remains opaque to core services."""

    id: str
    seq: int
    source_text: str
    source_hash: str
    format_metadata: dict[str, Any] = Field(default_factory=dict)


class DocumentIR(BaseModel):
    id: str
    filename: str
    format: str
    size_bytes: int
    page_count: int | None = None
    blocks: list[Block]


class TranslationPlan(BaseModel):
    source_language: str
    domain: str
    register: str
    terms: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    triage_status: TriageStatus = TriageStatus.OK


class ChunkRequest(BaseModel):
    chunk_id: str
    blocks: list[Block]
    target_language: str
    plan: TranslationPlan
    glossary: dict[str, str] = Field(default_factory=dict)
    model: str


class ChunkResult(BaseModel):
    translations: dict[str, str]
    tokens_in: int
    tokens_out: int
    model: str


class JobError(BaseModel):
    error_code: str
    message: str
    retryable: bool


class DocumentRecord(BaseModel):
    id: str
    filename: str
    format: str
    size_bytes: int
    page_count: int | None
    storage_path: str
    status: DocumentStatus
    error_code: str | None
    created_at: datetime


class JobRecord(BaseModel):
    id: str
    document_id: str
    batch_id: str
    target_language: str
    status: JobStatus
    total_chunks: int
    done_chunks: int
    model: str
    prompt_version: str
    glossary: dict[str, str]
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
    id: str
    job_id: str
    seq: int
    status: ChunkStatus
    lease_owner: str | None
    lease_expires_at: datetime | None
    created_at: datetime


class ChunkAttemptRecord(BaseModel):
    id: str
    chunk_id: str
    attempt_no: int
    tokens_in: int
    tokens_out: int
    cost_usd: float
    latency_ms: int
    outcome: str
    error_detail: str | None
    created_at: datetime


class BlockTranslationRecord(BaseModel):
    translation_key: str
    block_id: str
    translated_text: str
    created_at: datetime
