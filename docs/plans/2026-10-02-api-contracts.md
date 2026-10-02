# API Contracts and Domain Models Plan

**Date:** 2026-10-02  
**Status:** Implemented (contract stage), including the approved 2026-10-02 transaction and opaque-metadata corrections.  
**Based on:** ARCHITECTURE.md: “Layering & the Document IR”, “Data model (SQLite, WAL)”, “LLM provider & the agent question”, “REST API surface”, and “MCP server”; follow-up clarifications.

## Decisions

- **Option B:** core models are separate from REST/MCP schemas. The core does not depend on FastAPI.
- **Format ports:** `FormatAdapter` is rejected. We keep strict separation of `DocumentExtractor` and `DocumentRenderer`. `FormatRegistry` is introduced at the application level to map file extensions to concrete port implementations.
- **Repositories:** `JobRepository` is split into:
  - `DocumentRepository` — documents, blocks, triage analysis;
  - `JobExecutionRepository` — jobs, chunks, attempts (changed within the same transactions);
  - `TranslationCacheRepository` — block translation cache.
- **Additional ports:** `FileStorage` (critical for disk-less tests) and `CostCalculator`.
- **Transaction management — Option A:** replace separate `create_job` and
  `create_chunks` methods with
  `create_job_with_chunks(job: JobRecord, chunks: list[ChunkRecord]) -> None`.
  A service invokes this aggregate operation; the SQLite implementation owns
  its single BEGIN/COMMIT boundary and rolls back the entire operation on
  failure. Neither a job without its chunks nor partial chunks may be committed.
  Unit of Work (transaction-management Option B) is deferred as unnecessary
  boilerplate for the MVP. This choice is independent of model-separation
  Option B above.
- **Opaque typing:** `Block.format_metadata` remains `dict[str, Any]`, equivalent
  to `Record<string, unknown>` for JSON payloads. Nested keys and values must
  survive Pydantic validation and serialization without format-specific
  validation, coercion, filtering, or normalization.
- **SQLite initialization (next persistence stage):** explicitly execute
  `PRAGMA journal_mode=WAL` during application startup, before accepting work,
  and on every new SQLite connection. Also set `synchronous=NORMAL`,
  `foreign_keys=ON`, and `busy_timeout=20000` on each connection. Do not rely on
  a database file's previous WAL setting. SQL and connection setup belong only
  in `app/adapters/persistence/`.

## Implementation Scope

This stage implements the domain models, ports, REST schemas, adapter skeletons,
and contract tests listed under “Next Steps After Approval”. Concrete SQLite,
format, LLM, storage, and pricing behavior, REST routes, and registered MCP tools
are later implementation stages. The MCP signatures below remain their approved
design; this stage does not register executable tools.

## File Layout

```
app/
  core/
    models.py          # domain Pydantic models, enums, record models
    ports.py           # all typing.Protocol definitions
  api/
    schemas.py         # request/response Pydantic models for REST
  mcp_server/
    tools.py           # MCP tools, reuses api/schemas.py
  adapters/
    formats/
      pdf.py           # implements DocumentExtractor + DocumentRenderer
      docx.py          # implements DocumentExtractor + DocumentRenderer
      registry.py      # FormatRegistry — binds format to (extractor, renderer) pairs
    persistence/
      sqlite/
        document_repo.py
        job_repo.py
        cache_repo.py
    storage/
      filesystem.py    # FileStorage
    llm/
      openai_provider.py
      fake_provider.py
      pricing.py       # CostCalculator
      triage_agent.py
```

## 1. Core Domain Models (`app/core/models.py`)

```python
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
    """The only document abstraction known to the core.
    format_metadata is an opaque JSON blob owned by the format adapter.
    """

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
    translations: dict[str, str]  # block_id -> translated_text
    tokens_in: int
    tokens_out: int
    model: str


class JobError(BaseModel):
    error_code: str
    message: str
    retryable: bool
```

## 2. Record Models for Repositories

```python
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
```

## 3. Core Ports (`app/core/ports.py`)

```python
from datetime import datetime
from pathlib import Path
from typing import Protocol

from app.core.models import (
    Block,
    ChunkAttemptRecord,
    ChunkRecord,
    ChunkRequest,
    ChunkResult,
    DocumentIR,
    DocumentRecord,
    DocumentStatus,
    JobError,
    JobRecord,
    JobStatus,
    TranslationPlan,
)


class LLMProvider(Protocol):
    async def translate_chunk(self, request: ChunkRequest) -> ChunkResult: ...


class DocumentExtractor(Protocol):
    async def extract(self, file_path: Path, document_id: str) -> DocumentIR: ...


class DocumentRenderer(Protocol):
    async def render(
        self,
        original_path: Path,
        blocks: list[Block],
        translations: dict[str, str],
        output_path: Path,
    ) -> Path: ...


class TriageAgent(Protocol):
    async def analyze(self, document: DocumentIR) -> TranslationPlan: ...


class DocumentRepository(Protocol):
    # Documents
    async def create_document(
        self,
        id: str,
        filename: str,
        format: str,
        size_bytes: int,
        storage_path: str,
        page_count: int | None = None,
    ) -> DocumentRecord: ...

    async def get_document(self, document_id: str) -> DocumentRecord | None: ...

    async def update_document_status(
        self,
        document_id: str,
        status: DocumentStatus,
        error_code: str | None = None,
    ) -> None: ...

    # Blocks
    async def create_blocks(self, document_id: str, blocks: list[Block]) -> None: ...

    async def get_blocks(self, document_id: str) -> list[Block]: ...

    # Triage analysis
    async def save_analysis(self, document_id: str, plan: TranslationPlan) -> None: ...

    async def get_analysis(self, document_id: str) -> TranslationPlan | None: ...


class JobExecutionRepository(Protocol):
    # Jobs
    async def create_job_with_chunks(
        self,
        job: JobRecord,
        chunks: list[ChunkRecord],
    ) -> None:
        """Persist the job and all chunks in one atomic transaction.

        The persistence implementation owns BEGIN/COMMIT and rolls back the
        entire operation on failure. A service initiates this aggregate write.
        """
        ...

    async def get_job(self, job_id: str) -> JobRecord | None: ...

    async def claim_job(
        self,
        worker_id: str,
        lease_expires_at: datetime,
    ) -> JobRecord | None: ...

    async def heartbeat_job(
        self,
        job_id: str,
        lease_expires_at: datetime,
    ) -> None: ...

    async def update_job_progress(
        self,
        job_id: str,
        done_chunks: int,
    ) -> None: ...

    async def complete_job(
        self,
        job_id: str,
        status: JobStatus,
        error: JobError | None = None,
    ) -> None: ...

    async def get_jobs_by_batch(self, batch_id: str) -> list[JobRecord]: ...

    # Chunks
    async def get_pending_chunks(self, job_id: str) -> list[ChunkRecord]: ...

    async def claim_chunk(
        self,
        job_id: str,
        worker_id: str,
        lease_expires_at: datetime,
    ) -> ChunkRecord | None: ...

    async def heartbeat_chunk(
        self,
        chunk_id: str,
        lease_expires_at: datetime,
    ) -> None: ...

    async def complete_chunk(self, chunk_id: str) -> None: ...

    async def release_expired_chunks(
        self,
        now: datetime,
    ) -> list[ChunkRecord]: ...

    # Attempts / cost
    async def record_chunk_attempt(
        self,
        attempt: ChunkAttemptRecord,
    ) -> None: ...


class TranslationCacheRepository(Protocol):
    async def get_block_translation(
        self,
        translation_key: str,
        block_id: str,
    ) -> str | None: ...

    async def save_block_translation(
        self,
        translation_key: str,
        block_id: str,
        translated_text: str,
    ) -> None: ...


class FileStorage(Protocol):
    async def save_upload(
        self,
        document_id: str,
        content: bytes,
        filename: str,
    ) -> Path: ...

    async def get_upload_path(self, document_id: str) -> Path: ...

    async def save_output(
        self,
        job_id: str,
        content: bytes,
        filename: str,
    ) -> Path: ...

    async def get_output_path(self, job_id: str) -> Path: ...


class CostCalculator(Protocol):
    def estimate(self, model: str, tokens_in: int, tokens_out: int) -> float: ...


class FormatRegistry(Protocol):
    """Binds a file format to a concrete extractor + renderer pair.
    Implementation lives in adapters/formats/registry.py.
    """

    def register(
        self,
        format_name: str,
        extractor: DocumentExtractor,
        renderer: DocumentRenderer,
    ) -> None: ...

    async def resolve(
        self,
        file_path: Path,
    ) -> tuple[DocumentExtractor, DocumentRenderer] | None: ...
```

## 4. REST API Schemas (`app/api/schemas.py`)

```python
from pydantic import BaseModel, Field

from app.core.models import DocumentStatus, JobError, JobStatus


class DocumentUploadResponse(BaseModel):
    id: str
    filename: str
    format: str
    status: DocumentStatus
    block_count: int


class CreateJobRequest(BaseModel):
    document_id: str
    target_languages: list[str] = Field(..., min_length=1)
    idempotency_key: str


class JobSummaryResponse(BaseModel):
    id: str
    document_id: str
    batch_id: str
    target_language: str
    status: JobStatus
    total_chunks: int
    done_chunks: int
    cost_usd: float
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
    cost_usd: float
    error: JobError | None


class ErrorResponse(BaseModel):
    error_code: str
    message: str
    retryable: bool
```

## 5. MCP Tools

Tools are defined as async functions; schemas are reused from `api/schemas.py`.

```python
async def translate_file(path: str, target_languages: list[str]) -> list[str]: ...
async def check_status(job_id: str) -> JobSummaryResponse: ...
async def download_result(job_id: str, output_dir: str) -> str: ...
async def list_recent_jobs(limit: int = 10) -> list[JobSummaryResponse]: ...
```

## 6. Rules to Be Enforced by Tests

- **Opaque Metadata invariant:** the core (chunker, worker, LLM, cache) never parses `Block.format_metadata`.
- **Opaque round trips:** arbitrary nested JSON keys and values, including metadata with invalid format-specific shapes, survive Pydantic Python and JSON round trips unchanged.
- **Atomic enqueue contract:** only `create_job_with_chunks` creates jobs and their chunks; independent creation methods are absent. Database rollback and per-connection PRAGMA integration tests accompany the future SQLite implementation.
- **Layering:** `app/core/` must not import from `api/`, `mcp_server/`, `worker/`, or `adapters/`.
- **Repository split:** `DocumentRepository`, `JobExecutionRepository`, and `TranslationCacheRepository` must not be merged into a single class.
- **Format isolation:** PDF and DOCX adapters implement only `DocumentExtractor`/`DocumentRenderer`; they know nothing about jobs or the queue.

## 7. Next Steps After Approval

1. Create `app/core/models.py` and `app/core/ports.py` (no implementations).
2. Create `app/api/schemas.py`.
3. Create skeletons for `adapters/formats/registry.py`, `adapters/storage/filesystem.py`, `adapters/llm/pricing.py`.
4. Write tests for the Opaque Metadata invariant and layering (grep/import lint).

## Implementation Result

Domain models and ports, REST schemas, and registry/storage/pricing skeletons
are implemented. Contract tests cover opaque Python/JSON round trips, nested
models, repository separation, atomic enqueue signatures, REST validation, and
AST guards for layering and format isolation. Future format adapters are checked
when added; the registry skeleton is checked now.

Validation: `make test` (25 passed), `make lint`, and `make typecheck`.
Pydantic emits a warning because the approved `TranslationPlan.register` field
shadows `BaseModel.register`; its name and serialized contract are preserved.
Concrete adapter behavior and SQLite transaction/WAL integration tests remain
the next implementation stages. Commits were subsequently authorized by the user;
their task mappings are recorded in `TASKS.md`.
