"""Interfaces implemented by application adapters."""

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

    async def save_analysis(self, document_id: str, plan: TranslationPlan) -> None: ...

    async def get_analysis(self, document_id: str) -> TranslationPlan | None: ...


class JobExecutionRepository(Protocol):
    async def create_job_with_chunks(
        self,
        job: JobRecord,
        chunks: list[ChunkRecord],
    ) -> None:
        """Persist a job and its chunks atomically.

        The persistence implementation manages the transaction for this
        aggregate and must roll it back if any part of the write fails.
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
