"""Pydantic request and response contracts for the REST API."""

from pydantic import BaseModel, ConfigDict, Field

from app.core.models import DocumentStatus, JobError, JobStatus


class DocumentUploadResponse(BaseModel):
    id: str
    filename: str
    format: str
    status: DocumentStatus
    block_count: int
    analysis_cost_usd: float = 0.0
    warnings: list[str] = Field(default_factory=list)


class CreateJobRequest(BaseModel):
    document_id: str
    target_languages: list[str] = Field(..., min_length=1)
    idempotency_key: str


class JobSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    document_id: str
    batch_id: str
    target_language: str
    status: JobStatus
    total_chunks: int
    done_chunks: int
    cache_hit_blocks: int = Field(default=0, ge=0)
    cache_miss_blocks: int = Field(default=0, ge=0)
    cost_usd: float
    #: Cumulative triage cost of the document, shared by every language. Zero
    #: means no usage was recorded, which is not the same as a free analysis.
    analysis_cost_usd: float = Field(default=0.0, ge=0.0, allow_inf_nan=False)
    error: JobError | None


class BatchResponse(BaseModel):
    batch_id: str
    jobs: list[JobSummaryResponse]


class RetryRequest(BaseModel):
    raised_cost_cap_usd: float | None = None


class ServerSentEvent(BaseModel):
    job_id: str
    event: str  # "status" | "progress" | "error" | "done"
    status: JobStatus
    done_chunks: int
    total_chunks: int
    cache_hit_blocks: int = Field(default=0, ge=0)
    cache_miss_blocks: int = Field(default=0, ge=0)
    cost_usd: float
    error: JobError | None


class ErrorResponse(BaseModel):
    error_code: str
    message: str
    retryable: bool
