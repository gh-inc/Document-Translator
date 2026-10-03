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
    RenderResult,
    TranslationPlan,
    TriageResult,
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
    ) -> RenderResult: ...


class TriageAgent(Protocol):
    async def analyze(self, document: DocumentIR) -> TriageResult: ...


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
        *,
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost_usd: float = 0.0,
        cost_usd_total: float | None = None,
        tokens_in_total: int | None = None,
        tokens_out_total: int | None = None,
    ) -> DocumentAnalysisRecord: ...

    async def get_analysis(self, document_id: str) -> DocumentAnalysisRecord | None: ...


class JobExecutionRepository(Protocol):
    async def create_job_with_chunks(
        self,
        job: JobRecord,
        chunks: list[ChunkRecord],
        chunk_blocks: list[ChunkBlockRecord],
    ) -> None:
        """Persist a job, its chunks, and their block links atomically.

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

    async def record_job_cache_counts(self, job_id: str, *, hits: int, misses: int) -> None: ...

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
        source_hash: str,
    ) -> str | None: ...

    async def save_block_translation(
        self,
        translation_key: str,
        source_hash: str,
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

    def estimate_usage(
        self, model: str, tokens_in: int, tokens_out: int, cached_tokens_in: int = 0
    ) -> float: ...


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
