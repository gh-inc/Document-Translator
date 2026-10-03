"""Lifecycle and short-lived adapter composition for MCP tool operations."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import structlog

from app.adapters.formats.docx import DocxExtractor, DocxRenderer
from app.adapters.formats.markdown import MarkdownExtractor, MarkdownRenderer
from app.adapters.formats.pdf import PdfExtractor, PdfRenderer
from app.adapters.formats.registry import FormatRegistry
from app.adapters.llm.pricing import ModelCostCalculator
from app.adapters.llm.triage_runtime import (
    AgentFactory,
    ClaimedTriage,
    create_triage_agent,
    prepare_triage,
)
from app.adapters.persistence.api import ApiJobExecutionRepository, ApiPersistence
from app.adapters.persistence.database import SqliteConnectionFactory, _finish_cleanup, transaction
from app.adapters.persistence.repositories import (
    SqliteDocumentRepository,
    SqliteTranslationCacheRepository,
)
from app.adapters.persistence.triage import TriagePersistence
from app.adapters.storage.document_locks import document_upload_lock
from app.adapters.storage.filesystem import FilesystemStorage
from app.config import Settings
from app.core.errors import ErrorCode, ServiceError
from app.core.models import Block, DocumentRecord, DocumentStatus
from app.core.services.document_service import DocumentService
from app.core.services.job_service import JobService

logger = structlog.get_logger(__name__)


@dataclass
class Services:
    documents: DocumentService
    jobs: JobService
    document_repo: SqliteDocumentRepository


class McpRuntime:
    """Own background analysis tasks; never retain an idle database connection."""

    def __init__(
        self,
        settings: Settings,
        *,
        agent_factory: AgentFactory = create_triage_agent,
    ) -> None:
        self.settings = settings
        self.agent_factory = agent_factory
        self.factory = SqliteConnectionFactory(settings.database_path)
        self.storage = FilesystemStorage(settings.upload_storage_path, settings.output_storage_path)
        self.formats = FormatRegistry()
        self.formats.register("pdf", PdfExtractor(), PdfRenderer())
        self.formats.register("docx", DocxExtractor(), DocxRenderer())
        self.formats.register("md", MarkdownExtractor(), MarkdownRenderer())
        self.upload_lock = asyncio.Lock()
        self._triage_tasks: set[asyncio.Task[None]] = set()
        self._triage_claims: dict[asyncio.Task[None], ClaimedTriage] = {}
        self._cleanup_tasks: set[asyncio.Task[None]] = set()
        self._readiness_tasks: set[asyncio.Task[DocumentRecord]] = set()
        self._closed = False

    async def startup(self) -> None:
        """Initialize WAL before accepting any tool calls."""
        for directory in (
            self.settings.mcp_shared_dir,
            self.settings.upload_storage_path,
            self.settings.output_storage_path,
        ):
            await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
        await self._report_shared_directory()
        connection = await self.factory.create()
        await _finish_cleanup(connection.close())

    async def _report_shared_directory(self) -> None:
        """Log one actionable line when a completed translation cannot be written.

        `mkdir(exist_ok=True)` succeeds against a directory owned by another
        uid, so an unusable share looks healthy until the first download. Reads
        still work, so this must never stop the server: `translate_file` and
        `check_status` stay usable and only `download_result` is affected. The
        remedy is host-side ownership and the service never changes host
        permissions itself; it provisions the output directory on first use when
        the mount root allows it. Only the child directory name is logged, never
        a resolved host path or a listing.
        """
        root = self.settings.mcp_shared_dir
        probe = root / "output"
        root_writable = await asyncio.to_thread(os.access, root, os.W_OK)
        # A missing output directory is the healthy case: the download path
        # creates it under the service uid. Only an existing directory that the
        # service cannot write is a fault.
        output_exists = await asyncio.to_thread(probe.exists)
        if not output_exists:
            if not root_writable:
                logger.error(
                    "mcp_shared_dir_not_writable",
                    uid=os.getuid(),
                    directory_name=probe.name,
                    remedy=(
                        "grant the service uid write access to the host shared "
                        "directory; downloads cannot create their output directory"
                    ),
                )
            return
        if await asyncio.to_thread(os.access, probe, os.W_OK):
            return
        logger.error(
            "mcp_shared_dir_not_writable",
            uid=os.getuid(),
            directory_name=probe.name,
            remedy=(
                "grant the service uid write access to the host shared directory, "
                "or remove the output directory so the service creates it itself"
            ),
        )

    @asynccontextmanager
    async def services(self) -> AsyncIterator[Services]:
        connection = await self.factory.create()
        try:
            documents = SqliteDocumentRepository(connection)
            persistence = ApiPersistence(connection)

            async def resolve_skip_block_ids(
                document: DocumentRecord, blocks: list[Block]
            ) -> set[str]:
                return self.formats.get_skip_block_ids(document.format, blocks)

            yield Services(
                documents=DocumentService(
                    documents,
                    self.storage,
                    self.formats,
                    lambda: transaction(connection),
                    self.settings,
                    cleanup_upload=self.storage.remove_upload,
                    upload_lock=self.upload_lock,
                    analysis_in_use=TriagePersistence(connection).has_jobs,
                    upload_context=lambda document_id: document_upload_lock(
                        self.settings.database_path, document_id
                    ),
                ),
                jobs=JobService(
                    documents,
                    ApiJobExecutionRepository(connection),
                    SqliteTranslationCacheRepository(connection),
                    ModelCostCalculator(),
                    persistence=persistence,
                    settings=self.settings,
                    skip_block_ids_resolver=resolve_skip_block_ids,
                ),
                document_repo=documents,
            )
        finally:
            await _finish_cleanup(connection.close())

    async def get_document(self, document_id: str) -> DocumentRecord | None:
        async with self.services() as services:
            return await services.document_repo.get_document(document_id)

    def start_readiness(self, document: DocumentRecord) -> asyncio.Task[DocumentRecord]:
        """Own readiness cancellation cleanup separately from the call deadline."""
        task = asyncio.create_task(self._wait_for_analysis(document))
        self._readiness_tasks.add(task)
        task.add_done_callback(self._readiness_finished)
        return task

    async def _wait_for_analysis(self, document: DocumentRecord) -> DocumentRecord:
        if document.status is DocumentStatus.ANALYZING:
            await self.schedule_triage(document.id)
        while document.status not in {DocumentStatus.EXTRACTED, DocumentStatus.FAILED}:
            latest = await self.get_document(document.id)
            if latest is None:
                raise ServiceError(ErrorCode.NOT_FOUND, status_code=404)
            document = latest
            if document.status not in {DocumentStatus.EXTRACTED, DocumentStatus.FAILED}:
                await asyncio.sleep(self.settings.mcp_triage_poll_interval_seconds)
        return document

    def _readiness_finished(self, task: asyncio.Task[DocumentRecord]) -> None:
        self._readiness_tasks.discard(task)
        if not task.cancelled():
            task.exception()  # Consume errors even after the caller's deadline.

    async def schedule_triage(self, document_id: str) -> None:
        if self._closed:
            raise RuntimeError("MCP runtime is closed")
        claimed = await prepare_triage(document_id, self.settings, agent_factory=self.agent_factory)
        if claimed is None:
            return
        if self._closed:
            await claimed.close()
            return
        try:
            task = asyncio.create_task(claimed.run(), name=f"mcp-triage-{document_id}")
        except BaseException:
            await claimed.close()
            raise
        self._triage_tasks.add(task)
        self._triage_claims[task] = claimed
        task.add_done_callback(self._triage_finished)

    def _triage_finished(self, task: asyncio.Task[None]) -> None:
        self._triage_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.warning("mcp_triage_failed")
        # A task cancelled before its first instruction never executes run's
        # finally block. Keep separate cleanup ownership for that boundary.
        claimed = self._triage_claims.pop(task, None)
        if claimed is not None:
            cleanup = asyncio.create_task(claimed.close())
            self._cleanup_tasks.add(cleanup)
            cleanup.add_done_callback(self._cleanup_tasks.discard)

    async def aclose(self) -> None:
        self._closed = True
        readiness = list(self._readiness_tasks)
        for pending in readiness:
            pending.cancel()
        if readiness:
            await asyncio.gather(*readiness, return_exceptions=True)
        tasks = list(self._triage_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for claimed in list(self._triage_claims.values()):
            await claimed.close()
        self._triage_claims.clear()
        if self._cleanup_tasks:
            await asyncio.gather(*self._cleanup_tasks, return_exceptions=True)
        self._triage_tasks.clear()
