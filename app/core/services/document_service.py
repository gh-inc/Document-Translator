"""Application service for validating and persisting uploaded documents."""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, nullcontext
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings
from app.core.errors import DocumentError, ErrorCode, ServiceError
from app.core.models import DocumentAnalysisRecord, DocumentRecord, DocumentStatus, TriageStatus
from app.core.ports import DocumentRepository, FileStorage, FormatRegistry

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_DOCUMENT_PAGES = 400
MAX_EXTRACTED_TEXT_BYTES = 10 * 1024 * 1024
_SUPPORTED_FORMATS = frozenset({"docx", "md", "pdf"})
_SAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._ -]")

TransactionContext = Callable[[], AbstractAsyncContextManager[object]]
UploadCleanup = Callable[[str], Awaitable[None]]
UploadContext = Callable[[str], AbstractAsyncContextManager[object]]


class UploadResult(BaseModel):
    """Upload outcome, including ephemeral format-adapter warnings."""

    model_config = ConfigDict(extra="forbid")

    document: DocumentRecord
    block_count: int
    analysis_cost_usd: float = 0.0
    warnings: list[str] = Field(default_factory=list)


class DocumentService:
    """Save, extract, register, and read persisted documents."""

    def __init__(
        self,
        document_repo: DocumentRepository,
        file_storage: FileStorage,
        format_registry: FormatRegistry,
        transaction_context: TransactionContext,
        settings: Settings,
        cleanup_upload: UploadCleanup | None = None,
        upload_lock: asyncio.Lock | None = None,
        analysis_in_use: Callable[[str], Awaitable[bool]] | None = None,
        upload_context: UploadContext | None = None,
    ) -> None:
        self._document_repo = document_repo
        self._file_storage = file_storage
        self._format_registry = format_registry
        self._transaction_context = transaction_context
        self._settings = settings
        self._cleanup_upload = cleanup_upload
        self._upload_lock = upload_lock or asyncio.Lock()
        self._analysis_in_use = analysis_in_use
        self._upload_context = upload_context

    async def get_document(
        self, document_id: str
    ) -> tuple[DocumentRecord, int, DocumentAnalysisRecord | None]:
        """Return the document, its persisted block count, and analysis record."""
        document = await self._document_repo.get_document(document_id)
        if document is None:
            raise ServiceError(ErrorCode.NOT_FOUND, status_code=404)
        blocks = await self._document_repo.get_blocks(document_id)
        analysis = await self._document_repo.get_analysis(document_id)
        return document, len(blocks), analysis

    async def upload(self, filename: str, content: bytes) -> UploadResult:
        """Validate and persist an upload, returning adapter warnings without persisting them."""
        async with self._upload_lock:
            return await self._upload(filename, content)

    async def _upload(self, filename: str, content: bytes) -> UploadResult:
        safe_filename = sanitize_filename(filename)
        if len(content) > MAX_UPLOAD_BYTES:
            raise ServiceError(ErrorCode.FILE_TOO_LARGE, status_code=413)

        file_format = Path(safe_filename).suffix.lower().lstrip(".")
        if file_format not in _SUPPORTED_FORMATS:
            raise ServiceError(ErrorCode.UNSUPPORTED_FORMAT, status_code=415)

        document_id = await asyncio.to_thread(lambda: hashlib.sha256(content).hexdigest())
        context = self._upload_context(document_id) if self._upload_context else nullcontext()
        async with context:
            return await self._upload_document(document_id, safe_filename, file_format, content)

    async def _upload_document(
        self, document_id: str, safe_filename: str, file_format: str, content: bytes
    ) -> UploadResult:
        existing = await self._document_repo.get_document(document_id)
        if existing is not None:
            if existing.format != file_format:
                raise DocumentError(ErrorCode.CORRUPT_FILE)
            # Warnings are ephemeral. Regenerate them through the format port
            # on duplicate uploads, without interpreting opaque block metadata.
            upload_path = await self._file_storage.get_upload_path(document_id)
            resolved = await self._format_registry.resolve(upload_path)
            if resolved is None:
                raise DocumentError(ErrorCode.CORRUPT_FILE)
            extractor, _renderer = resolved
            document_ir = await extractor.extract(upload_path, document_id)
            analysis = await self._document_repo.get_analysis(document_id)
            return UploadResult(
                document=existing,
                block_count=len(await self._document_repo.get_blocks(document_id)),
                analysis_cost_usd=analysis.cost_usd_total if analysis is not None else 0.0,
                warnings=document_ir.warnings,
            )
        save_task: asyncio.Task[Path] | None = None
        try:
            save_task = asyncio.create_task(
                self._file_storage.save_upload(document_id, content, safe_filename)
            )
            # FilesystemStorage delegates the write to a worker thread. Shield
            # it so cancellation cannot abandon a write that may still publish.
            upload_path = await asyncio.shield(save_task)
            resolved = await self._format_registry.resolve(upload_path)
            if resolved is None:
                # The extension was whitelisted; a missing registry match now
                # means that its signature could not be read or did not match.
                raise DocumentError(ErrorCode.CORRUPT_FILE)
            extractor, _renderer = resolved

            # Format adapters own any blocking work and already offload it.
            document_ir = await extractor.extract(upload_path, document_id)
            if document_ir.page_count is not None and document_ir.page_count > MAX_DOCUMENT_PAGES:
                raise DocumentError(ErrorCode.PAGE_LIMIT)
            try:
                extracted_text_bytes = sum(
                    len(block.source_text.encode("utf-8")) for block in document_ir.blocks
                )
            except UnicodeEncodeError:
                raise DocumentError(ErrorCode.CORRUPT_FILE) from None
            if extracted_text_bytes > MAX_EXTRACTED_TEXT_BYTES:
                raise DocumentError(ErrorCode.TEXT_LIMIT)

            async with self._transaction_context():
                document = await self._document_repo.create_document(
                    id=document_id,
                    filename=safe_filename,
                    format=file_format,
                    size_bytes=len(content),
                    storage_path=str(upload_path),
                    page_count=document_ir.page_count,
                )
                await self._document_repo.create_blocks(document_id, document_ir.blocks)
                await self._document_repo.update_document_status(
                    document_id,
                    DocumentStatus.ANALYZING,
                )
            document = document.model_copy(update={"status": DocumentStatus.ANALYZING})
            return UploadResult(
                document=document,
                block_count=len(document_ir.blocks),
                warnings=document_ir.warnings,
            )
        except BaseException:
            if save_task is not None:
                await _finish_task(save_task)
            await self._cleanup(document_id)
            raise

    async def retry_triage(
        self, document_id: str
    ) -> tuple[DocumentRecord, int, DocumentAnalysisRecord | None]:
        """Explicitly recover analysis without resetting a successful plan."""
        async with self._transaction_context():
            document = await self._document_repo.get_document(document_id)
            if document is None:
                raise ServiceError(ErrorCode.NOT_FOUND, status_code=404)
            analysis = await self._document_repo.get_analysis(document_id)
            if analysis is None or analysis.triage_status is TriageStatus.DEGRADED:
                if self._analysis_in_use is not None and await self._analysis_in_use(document_id):
                    # Existing and resumed jobs must keep their persisted plan
                    # consistent with the semantic translation cache.
                    raise ServiceError(ErrorCode.CONFLICT, status_code=409)
                if document.status is DocumentStatus.FAILED:
                    raise ServiceError(ErrorCode.CONFLICT, status_code=409)
                # The shared scheduler commits the conditional claim before
                # scheduling work. This service validates retry eligibility;
                # it must not reset status while another process owns triage.
                document = document.model_copy(update={"status": DocumentStatus.ANALYZING})
            blocks = await self._document_repo.get_blocks(document_id)
        return document, len(blocks), analysis

    async def _cleanup(self, document_id: str) -> None:
        cleanup_callback = self._cleanup_upload
        if cleanup_callback is None:
            return

        async def cleanup_upload() -> None:
            await cleanup_callback(document_id)

        cleanup_task = asyncio.create_task(cleanup_upload())
        await _finish_task(cleanup_task)


async def _finish_task(task: asyncio.Task[object]) -> None:
    """Wait through cancellation and consume a best-effort task's result."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            current_task = asyncio.current_task()
            if current_task is not None and current_task.cancelling():
                current_task.uncancel()
        except BaseException:
            # Task exceptions mean it has finished; the result is consumed below.
            continue
    try:
        task.result()
    except BaseException:
        # Preserve the original upload/extraction/persistence failure.
        return


def sanitize_filename(filename: str) -> str:
    """Return a deterministic safe basename across POSIX and Windows paths."""
    basename = filename.replace("\\", "/").rsplit("/", maxsplit=1)[-1].strip()
    basename = _SAFE_FILENAME_CHARS.sub("_", basename).strip(" .")
    if not basename:
        raise ServiceError(ErrorCode.INVALID_REQUEST, status_code=400)

    # Bound a path component while retaining its suffix for format resolution.
    suffix = Path(basename).suffix[:20]
    stem = basename[: -len(Path(basename).suffix)] if Path(basename).suffix else basename
    if len(basename.encode("utf-8")) > 200:
        max_stem_chars = max(1, 180 - len(suffix.encode("utf-8")))
        stem = stem[:max_stem_chars]
        basename = f"{stem}{suffix}"
    if basename in {".", ".."}:
        raise ServiceError(ErrorCode.INVALID_REQUEST, status_code=400)
    return basename
