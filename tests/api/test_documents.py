"""Document status reads through the real ASGI and WAL persistence paths."""

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from app.adapters.persistence.database import SqliteConnectionFactory, transaction
from app.adapters.persistence.repositories import SqliteDocumentRepository
from app.api.main import create_app
from app.config import Settings
from app.core.models import Block, DocumentStatus


@pytest.fixture
async def runtime(tmp_path: Path) -> AsyncIterator[tuple[Settings, httpx.AsyncClient]]:
    settings = Settings(
        database_path=tmp_path / "documents.db",
        upload_storage_path=tmp_path / "uploads",
        output_storage_path=tmp_path / "out",
        llm_provider="fake",
    )
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield settings, client


@pytest.mark.parametrize("status", ["analyzing", "extracted", "failed"])
async def test_document_status_returns_persisted_state_and_own_block_count(
    runtime: tuple[Settings, httpx.AsyncClient], status: str
) -> None:
    settings, client = runtime
    connection = await SqliteConnectionFactory(settings.database_path).create()
    try:
        repo = SqliteDocumentRepository(connection)
        async with transaction(connection):
            await repo.create_document("report", "report.docx", "docx", 20, "/private/report")
            await repo.create_blocks(
                "report",
                [
                    Block(id="a", seq=0, source_text="First", source_hash="first"),
                    Block(id="b", seq=1, source_text="Second", source_hash="second"),
                ],
            )
            await repo.update_document_status("report", DocumentStatus(status))
            await repo.create_document("other", "other.pdf", "pdf", 10, "/private/other")
            await repo.create_blocks(
                "other", [Block(id="c", seq=0, source_text="Other", source_hash="other")]
            )

        response = await client.get("/api/documents/report")

        assert response.status_code == 200
        assert response.json() == {
            "id": "report",
            "filename": "report.docx",
            "format": "docx",
            "status": status,
            "block_count": 2,
        }
        # Reading readiness must not claim or start analysis.
        document = await repo.get_document("report")
        assert document is not None
        assert document.status is DocumentStatus(status)
        assert await repo.get_analysis("report") is None
    finally:
        await connection.close()


async def test_document_status_reports_zero_for_no_persisted_blocks(
    runtime: tuple[Settings, httpx.AsyncClient],
) -> None:
    settings, client = runtime
    connection = await SqliteConnectionFactory(settings.database_path).create()
    try:
        repo = SqliteDocumentRepository(connection)
        async with transaction(connection):
            await repo.create_document("empty", "empty.pdf", "pdf", 10, "/private/empty")
            await repo.update_document_status("empty", DocumentStatus.FAILED, "corrupt_file")
    finally:
        await connection.close()

    response = await client.get("/api/documents/empty")

    assert response.status_code == 200
    assert response.json() == {
        "id": "empty",
        "filename": "empty.pdf",
        "format": "pdf",
        "status": "failed",
        "block_count": 0,
    }


async def test_unknown_document_status_returns_catalogued_not_found(
    runtime: tuple[Settings, httpx.AsyncClient],
) -> None:
    _settings, client = runtime

    response = await client.get("/api/documents/missing")

    assert response.status_code == 404
    assert response.json() == {
        "error_code": "not_found",
        "message": "Requested resource was not found",
        "retryable": False,
    }
