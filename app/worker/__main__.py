"""Worker process startup and graceful signal handling."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from contextlib import suppress

import structlog

from app.adapters.formats.docx import DocxExtractor, DocxRenderer
from app.adapters.formats.pdf import PdfExtractor, PdfRenderer
from app.adapters.formats.registry import FormatRegistry
from app.adapters.llm.fake_provider import FakeProvider
from app.adapters.llm.openai_provider import OpenAIProvider
from app.adapters.llm.pricing import ModelCostCalculator
from app.adapters.persistence.database import SqliteConnectionFactory
from app.adapters.persistence.repositories import (
    SqliteDocumentRepository,
    SqliteJobExecutionRepository,
    SqliteTranslationCacheRepository,
)
from app.adapters.persistence.worker import WorkerPersistence
from app.adapters.storage.filesystem import FilesystemStorage
from app.config import Settings
from app.core.ports import LLMProvider
from app.worker.claim_loop import ClaimLoop

_logger = structlog.get_logger(__name__)


async def run_worker(
    settings: Settings | None = None,
    *,
    shutdown_event: asyncio.Event | None = None,
) -> None:
    """Create adapters, run the claim loop, and close process resources."""
    _configure_logging()
    configured = settings or Settings()
    connection = await SqliteConnectionFactory(configured.database_path).create()
    provider: LLMProvider | None = None
    installed_signals: list[signal.Signals] = []
    loop = asyncio.get_running_loop()
    try:
        if configured.llm_provider == "fake":
            provider = FakeProvider(settings=configured)
        else:
            provider = OpenAIProvider(settings=configured)

        document_repo = SqliteDocumentRepository(connection)
        job_repo = SqliteJobExecutionRepository(connection, worker_id=configured.worker_id)
        cache_repo = SqliteTranslationCacheRepository(connection)
        persistence = WorkerPersistence(connection)
        storage = FilesystemStorage(
            configured.upload_storage_path,
            configured.output_storage_path,
        )
        formats = FormatRegistry()
        formats.register("pdf", PdfExtractor(), PdfRenderer())
        formats.register("docx", DocxExtractor(), DocxRenderer())

        own_shutdown_event = shutdown_event is None
        stop = shutdown_event if shutdown_event is not None else asyncio.Event()
        if own_shutdown_event:
            for signal_number in (signal.SIGTERM, signal.SIGINT):
                try:
                    loop.add_signal_handler(signal_number, stop.set)
                    installed_signals.append(signal_number)
                except (NotImplementedError, RuntimeError, ValueError):
                    # The worker remains usable on event loops that do not support
                    # signal handlers; process supervision still owns termination.
                    continue

        claim_loop = ClaimLoop(
            configured,
            job_repo,
            cache_repo,
            document_repo,
            provider,
            ModelCostCalculator(),
            formats,
            storage,
            persistence=persistence,
            shutdown_event=stop,
        )
        _logger.info(
            "worker_started",
            worker_id=configured.worker_id,
            provider=configured.llm_provider,
        )
        sys.stdout.flush()
        await claim_loop.run()
    finally:
        for signal_number in installed_signals:
            with suppress(RuntimeError, ValueError):
                loop.remove_signal_handler(signal_number)
        try:
            close_provider = getattr(provider, "aclose", None)
            if close_provider is not None:
                await close_provider()
        finally:
            await connection.close()


def main() -> None:
    """Run the async worker until a shutdown signal is received."""
    asyncio.run(run_worker())


def _configure_logging() -> None:
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


if __name__ == "__main__":
    main()
