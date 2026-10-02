"""Application service for validating and persisting uploaded documents."""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path

from app.config import Settings
from app.core.errors import DocumentError, ErrorCode, ServiceError
from app.core.models import DocumentRecord, DocumentStatus, TranslationPlan
from app.core.ports import DocumentRepository, FileStorage, FormatRegistry

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_DOCUMENT_PAGES = 400
MAX_EXTRACTED_TEXT_BYTES = 10 * 1024 * 1024
_SUPPORTED_FORMATS = frozenset({"docx", "pdf"})
_SAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._ -]")

TransactionContext = Callable[[], AbstractAsyncContextManager[object]]
UploadCleanup = Callable[[str], Awaitable[None]]


class DocumentService:
    """Save, extract, and atomically register an uploaded document."""

    def __init__(
        self,
        document_repo: DocumentRepository,
        file_storage: FileStorage,
        format_registry: FormatRegistry,
        transaction_context: TransactionContext,
        settings: Settings,
        cleanup_upload: UploadCleanup | None = None,
    ) -> None:
        self._document_repo = document_repo
        self._file_storage = file_storage
        self._format_registry = format_registry
        self._transaction_context = transaction_context
        self._settings = settings
        self._cleanup_upload = cleanup_upload

    async def upload(self, filename: str, content: bytes) -> tuple[DocumentRecord, int]:
        """Validate and persist one upload; return its record and extracted block count."""
        safe_filename = sanitize_filename(filename)
        if len(content) > MAX_UPLOAD_BYTES:
            raise ServiceError(ErrorCode.FILE_TOO_LARGE, status_code=413)

        file_format = Path(safe_filename).suffix.lower().lstrip(".")
        if file_format not in _SUPPORTED_FORMATS:
            raise ServiceError(ErrorCode.UNSUPPORTED_FORMAT, status_code=415)

        document_id = str(uuid.uuid4())
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
                    DocumentStatus.EXTRACTED,
                )
                await self._document_repo.save_analysis(
                    document_id,
                    TranslationPlan(
                        source_language="en",
                        domain="general",
                        register="neutral",
                        terms=[],
                        warnings=[],
                    ),
                )
            document = document.model_copy(update={"status": DocumentStatus.EXTRACTED})
            return document, len(document_ir.blocks)
        except BaseException:
            if save_task is not None:
                await _finish_task(save_task)
            await self._cleanup(document_id)
            raise

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
