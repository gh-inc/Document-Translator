"""Explicit structured results for the editor translation workflow."""

from pydantic import BaseModel, ConfigDict

from app.core.errors import ErrorCode, catalog_entry
from app.core.models import DocumentStatus, JobRecord, JobStatus


class ResultModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolError(ResultModel):
    error_code: ErrorCode
    message: str
    retryable: bool
    document_id: str | None = None


class TranslationSubmission(ResultModel):
    document_id: str
    batch_id: str
    job_ids: list[str]


class JobSummary(ResultModel):
    id: str
    document_id: str
    batch_id: str
    target_language: str
    status: JobStatus
    total_chunks: int
    done_chunks: int
    cost_usd: float
    error: ToolError | None


class DocumentStatusResult(ResultModel):
    document_id: str
    status: DocumentStatus
    error: ToolError | None
    next_action: str


class DownloadResult(ResultModel):
    job_id: str
    path: str


def safe_error(error_code: str, *, document_id: str | None = None) -> ToolError:
    """Suppress unknown stored codes and all internal exception details."""
    try:
        code = ErrorCode(error_code)
    except ValueError:
        code = ErrorCode.INTERNAL_ERROR
    message, retryable = catalog_entry(code.value)
    return ToolError(
        error_code=code,
        message=message,
        retryable=retryable,
        document_id=document_id,
    )


def job_summary(job: JobRecord) -> JobSummary:
    return JobSummary(
        id=job.id,
        document_id=job.document_id,
        batch_id=job.batch_id,
        target_language=job.target_language,
        status=job.status,
        total_chunks=job.total_chunks,
        done_chunks=job.done_chunks,
        cost_usd=job.cost_usd,
        error=safe_error(job.error_code) if job.error_code is not None else None,
    )
