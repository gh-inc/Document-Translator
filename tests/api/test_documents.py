"""Document status reads through the real ASGI and WAL persistence paths."""

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from app.adapters.persistence.database import SqliteConnectionFactory, transaction
from app.adapters.persistence.repositories import SqliteDocumentRepository
from app.api.main import create_app
from app.config import Settings
from app.core.models import Block, DocumentStatus, TranslationPlan, TriageStatus

SAMPLES = Path(__file__).resolve().parents[2] / "samples"


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
            "analysis_cost_usd": 0.0,
            "warnings": [],
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
        "analysis_cost_usd": 0.0,
        "warnings": [],
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


async def test_document_payload_reports_cumulative_analysis_cost(
    runtime: tuple[Settings, httpx.AsyncClient],
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
            await repo.save_analysis(
                "report",
                TranslationPlan(source_language="en", domain="general", register="neutral"),
                cost_usd=0.0011,
                cost_usd_total=0.0034,
            )
        response = await client.get("/api/documents/report")
    finally:
        await connection.close()

    assert response.status_code == 200
    assert response.json()["analysis_cost_usd"] == 0.0034


async def test_fresh_upload_reports_zero_and_existing_cost_on_duplicate_and_retry(
    runtime: tuple[Settings, httpx.AsyncClient],
) -> None:
    settings, client = runtime
    content = (SAMPLES / "sample_en.docx").read_bytes()
    first = await client.post("/api/documents", files={"file": ("report.docx", content)})

    assert first.status_code == 200
    assert first.json()["analysis_cost_usd"] == 0.0

    connection = await SqliteConnectionFactory(settings.database_path).create()
    try:
        repo = SqliteDocumentRepository(connection)
        async with transaction(connection):
            await repo.save_analysis(
                first.json()["id"],
                TranslationPlan(
                    source_language="en",
                    domain="general",
                    register="neutral",
                    triage_status=TriageStatus.DEGRADED,
                ),
                tokens_in=1,
                cost_usd=0.0011,
                cost_usd_total=0.0034,
            )
    finally:
        await connection.close()

    duplicate = await client.post("/api/documents", files={"file": ("report.docx", content)})
    retry = await client.post(f"/api/documents/{first.json()['id']}/retry-triage")

    assert duplicate.status_code == retry.status_code == 200
    assert duplicate.json()["analysis_cost_usd"] == 0.0034
    assert retry.json()["analysis_cost_usd"] == 0.0034


async def test_pdf_upload_returns_warnings_for_initial_and_duplicate_uploads_only(
    runtime: tuple[Settings, httpx.AsyncClient],
) -> None:
    _settings, client = runtime
    content = (SAMPLES / "platon-gliph.pdf").read_bytes()

    first = await client.post(
        "/api/documents", files={"file": ("glyph.pdf", content, "application/pdf")}
    )
    duplicate = await client.post(
        "/api/documents", files={"file": ("glyph.pdf", content, "application/pdf")}
    )

    assert first.status_code == duplicate.status_code == 200
    warnings = first.json()["warnings"]
    assert warnings
    assert all("U+" in warning for warning in warnings)
    assert duplicate.json()["warnings"] == warnings
    assert duplicate.json()["id"] == first.json()["id"]
    document_id = first.json()["id"]
    status = await client.get(f"/api/documents/{document_id}")
    retry = await client.post(f"/api/documents/{document_id}/retry-triage")
    assert status.status_code == retry.status_code == 200
    assert status.json()["warnings"] == retry.json()["warnings"] == []


@pytest.mark.parametrize("filename", ["platon-complex.pdf", "platon-gliph.docx"])
async def test_clean_pdf_and_docx_rest_uploads_return_empty_warnings(
    runtime: tuple[Settings, httpx.AsyncClient], filename: str
) -> None:
    _settings, client = runtime
    content = (SAMPLES / filename).read_bytes()

    response = await client.post("/api/documents", files={"file": (filename, content)})

    assert response.status_code == 200
    assert response.json()["analysis_cost_usd"] == 0.0
    assert response.json()["warnings"] == []
