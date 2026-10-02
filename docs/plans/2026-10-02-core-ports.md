# Core Ports Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Finalize `app/core/ports.py` so that `JobExecutionRepository.create_job_with_chunks` is the single aggregate enqueue operation and explicitly carries `chunk_blocks` for the join table.

**Architecture:** Thin `Protocol` definitions in `app/core/ports.py`; no business logic. The persistence adapter owns the atomic transaction that writes `jobs`, `chunks`, and `chunk_blocks` together.

**Tech Stack:** Python 3.12, `typing.Protocol`.

**Status:** Implemented and validated under DT-8. The combined suite passes
79 tests; `make lint` and `make typecheck` are clean.

---

## Task 1: Update `DocumentRepository.save_analysis` Return Type

**Files:**
- Modify: `app/core/ports.py`

Change return type from `None` to `DocumentAnalysisRecord` so the adapter can return the persisted record (including `created_at` from the database).

**Step 1: Update the signature**

```python
async def save_analysis(
    self,
    document_id: str,
    plan: TranslationPlan,
) -> DocumentAnalysisRecord: ...
```

**Step 2: Verify imports**

Ensure `DocumentAnalysisRecord` is imported in `app/core/ports.py`.

---

## Task 2: Adopt Option A for Aggregate Job Creation

**Files:**
- Modify: `app/core/ports.py`
- Modify: `tests/test_domain_contracts.py`

Add a third parameter `chunk_blocks: list[ChunkBlockRecord]` to `JobExecutionRepository.create_job_with_chunks`. Update contract tests to enforce the new signature and continue rejecting separate `create_job` / `create_chunks` methods.

**Step 1: Write the failing contract test**

```python
def test_job_creation_port_requires_chunk_blocks() -> None:
    import inspect
    from typing import get_type_hints

    method = JobExecutionRepository.create_job_with_chunks
    signature = inspect.signature(method)
    params = list(signature.parameters.values())
    assert [p.name for p in params] == ["self", "job", "chunks", "chunk_blocks"]

    hints = get_type_hints(method)
    assert hints["job"] is JobRecord
    assert hints["chunks"] == list[ChunkRecord]
    assert hints["chunk_blocks"] == list[ChunkBlockRecord]
    assert hints["return"] is type(None)
    assert "create_job" not in JobExecutionRepository.__dict__
    assert "create_chunks" not in JobExecutionRepository.__dict__
```

Run: `pytest tests/test_domain_contracts.py::test_job_creation_port_requires_chunk_blocks -v`
Expected: FAIL before implementation.

**Step 2: Update the port signature**

```python
async def create_job_with_chunks(
    self,
    job: JobRecord,
    chunks: list[ChunkRecord],
    chunk_blocks: list[ChunkBlockRecord],
) -> None: ...
```

Document in the docstring that the adapter writes `jobs`, `chunks`, and `chunk_blocks` in one transaction and rolls back on any failure.

**Step 3: Run tests**

Run: `pytest tests/test_domain_contracts.py -v`
Expected: PASS

---

## Task 3: Ensure Repository Boundaries Stay Clean

**Files:**
- Modify: `tests/test_domain_contracts.py`

Confirm that `DocumentRepository`, `JobExecutionRepository`, and `TranslationCacheRepository` still have no overlapping public method sets.

Run: `pytest tests/test_domain_contracts.py::test_repository_ports_are_split_by_ownership -v`
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
git add app/core/ports.py tests/test_domain_contracts.py
git commit -m "DT-8: feat(core): finalize repository ports with aggregate job creation"
```

---

## Final Code for `app/core/ports.py`

**Execution notes:** DT-8 implements the final code's analysis return types,
including `get_analysis -> DocumentAnalysisRecord | None`, and the required
`chunk_blocks` argument without a default. Contract tests in
`tests/test_domain_contracts.py` check both analysis methods, coroutine and type
signatures, and repository separation. Atomic persistence remains an obligation
for the later repository implementation. The main agent coordinates this task
with the available collaboration tools because `superpowers:executing-plans`
is unavailable.

```python
"""Interfaces implemented by application adapters."""

from datetime import datetime
from pathlib import Path
from typing import Protocol

from app.core.models import (
    Block,
    ChunkAttemptRecord,
    ChunkBlockRecord,
    ChunkRecord,
    ChunkRequest,
    ChunkResult,
    DocumentAnalysisRecord,
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

    async def create_blocks(self, document_id: str, blocks: list[Block]) -> None: ...

    async def get_blocks(self, document_id: str) -> list[Block]: ...

    async def save_analysis(
        self,
        document_id: str,
        plan: TranslationPlan,
    ) -> DocumentAnalysisRecord: ...

    async def get_analysis(self, document_id: str) -> DocumentAnalysisRecord | None: ...


class JobExecutionRepository(Protocol):
    async def create_job_with_chunks(
        self,
        job: JobRecord,
        chunks: list[ChunkRecord],
        chunk_blocks: list[ChunkBlockRecord],
    ) -> None:
        """Persist a job and its chunks atomically.

        The persistence implementation writes the job, its chunks, and the
        ``chunk_blocks`` join rows in a single transaction and rolls back the
        entire aggregate on any failure.
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
    """Resolve file formats to extractor and renderer port implementations."""

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
