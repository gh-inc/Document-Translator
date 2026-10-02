"""Per-request SQLite connections and explicit adapter-to-service composition."""

from collections.abc import AsyncIterator
from typing import Annotated

from aiosqlite import Connection
from fastapi import Depends, Request

from app.adapters.formats.docx import DocxExtractor, DocxRenderer
from app.adapters.formats.pdf import PdfExtractor, PdfRenderer
from app.adapters.formats.registry import FormatRegistry
from app.adapters.llm.fake_provider import FakeProvider
from app.adapters.llm.openai_provider import OpenAIProvider
from app.adapters.llm.pricing import ModelCostCalculator
from app.adapters.llm.triage_runtime import ClaimedTriage
from app.adapters.persistence.api import ApiJobExecutionRepository, ApiPersistence
from app.adapters.persistence.database import SqliteConnectionFactory, transaction
from app.adapters.persistence.repositories import (
    SqliteDocumentRepository,
    SqliteJobExecutionRepository,
    SqliteTranslationCacheRepository,
)
from app.adapters.persistence.triage import TriagePersistence
from app.adapters.storage.document_locks import document_upload_lock
from app.adapters.storage.filesystem import FilesystemStorage
from app.adapters.storage.readiness import check_storage_writable
from app.config import Settings
from app.core.errors import ErrorCode, ServiceError
from app.core.ports import LLMProvider
from app.core.services.document_service import DocumentService
from app.core.services.health_service import HealthService
from app.core.services.job_service import JobService


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


SettingsDependency = Annotated[Settings, Depends(get_settings)]


async def get_triage_claims() -> AsyncIterator[list[ClaimedTriage]]:
    """Release prepared ownership even if sending the response is cancelled."""
    claims: list[ClaimedTriage] = []
    try:
        yield claims
    finally:
        for claim in claims:
            await claim.close()


TriageClaimsDependency = Annotated[list[ClaimedTriage], Depends(get_triage_claims)]


async def get_db_connection(settings: SettingsDependency) -> AsyncIterator[Connection]:
    try:
        connection = await SqliteConnectionFactory(settings.database_path).create()
    except Exception:
        raise ServiceError(ErrorCode.NOT_READY, status_code=503) from None
    try:
        yield connection
    finally:
        await connection.close()


ConnectionDependency = Annotated[Connection, Depends(get_db_connection)]


def get_document_repo(connection: ConnectionDependency) -> SqliteDocumentRepository:
    return SqliteDocumentRepository(connection)


def get_job_repo(connection: ConnectionDependency) -> SqliteJobExecutionRepository:
    return ApiJobExecutionRepository(connection)


def get_cache_repo(connection: ConnectionDependency) -> SqliteTranslationCacheRepository:
    return SqliteTranslationCacheRepository(connection)


def get_api_persistence(connection: ConnectionDependency) -> ApiPersistence:
    return ApiPersistence(connection)


def get_file_storage(settings: SettingsDependency) -> FilesystemStorage:
    return FilesystemStorage(settings.upload_storage_path, settings.output_storage_path)


def get_format_registry() -> FormatRegistry:
    registry = FormatRegistry()
    registry.register("pdf", PdfExtractor(), PdfRenderer())
    registry.register("docx", DocxExtractor(), DocxRenderer())
    return registry


async def get_llm_provider(settings: SettingsDependency) -> AsyncIterator[LLMProvider]:
    if settings.llm_provider == "fake":
        yield FakeProvider(settings=settings)
    else:
        provider = OpenAIProvider(settings=settings)
        try:
            yield provider
        finally:
            await provider.aclose()


def get_cost_calculator() -> ModelCostCalculator:
    return ModelCostCalculator()


def get_document_service(
    request: Request,
    connection: Annotated[Connection, Depends(get_db_connection, scope="function")],
    settings: SettingsDependency,
    storage: Annotated[FilesystemStorage, Depends(get_file_storage)],
    registry: Annotated[FormatRegistry, Depends(get_format_registry)],
) -> DocumentService:
    # Upload/retry connections close before response background tasks run.
    # Streaming job routes retain their separate request-scoped connection.
    return DocumentService(
        SqliteDocumentRepository(connection),
        storage,
        registry,
        lambda: transaction(connection),
        settings,
        cleanup_upload=storage.remove_upload,
        upload_lock=request.app.state.upload_lock,
        analysis_in_use=TriagePersistence(connection).has_jobs,
        upload_context=lambda document_id: document_upload_lock(
            settings.database_path, document_id
        ),
    )


def get_job_service(
    settings: SettingsDependency,
    document_repo: Annotated[SqliteDocumentRepository, Depends(get_document_repo)],
    job_repo: Annotated[SqliteJobExecutionRepository, Depends(get_job_repo)],
    cache_repo: Annotated[SqliteTranslationCacheRepository, Depends(get_cache_repo)],
    calculator: Annotated[ModelCostCalculator, Depends(get_cost_calculator)],
    persistence: Annotated[ApiPersistence, Depends(get_api_persistence)],
) -> JobService:
    return JobService(
        document_repo, job_repo, cache_repo, calculator, persistence=persistence, settings=settings
    )


def get_health_service(
    settings: SettingsDependency,
    persistence: Annotated[ApiPersistence, Depends(get_api_persistence)],
) -> HealthService:
    return HealthService(
        persistence,
        lambda: check_storage_writable(settings.upload_storage_path, settings.output_storage_path),
        stale_chunk_grace_seconds=max(120, 2 * settings.chunk_lease_seconds),
    )
