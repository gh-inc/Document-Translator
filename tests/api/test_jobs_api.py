"""End-to-end coverage for jobs, batch, retry, SSE, and download routes."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import fitz
import httpx
import pytest
from aiosqlite import Connection

from app.adapters.formats.docx import DocxExtractor, DocxRenderer
from app.adapters.formats.markdown import MarkdownExtractor, MarkdownRenderer
from app.adapters.formats.pdf import PdfExtractor, PdfRenderer
from app.adapters.formats.registry import FormatRegistry
from app.adapters.llm.fake_provider import FakeProvider
from app.adapters.llm.pricing import ModelCostCalculator
from app.adapters.persistence.database import SqliteConnectionFactory
from app.adapters.persistence.repositories import (
    SqliteDocumentRepository,
    SqliteJobExecutionRepository,
    SqliteTranslationCacheRepository,
)
from app.adapters.persistence.worker import WorkerPersistence
from app.adapters.storage.filesystem import FilesystemStorage
from app.api.dependencies import get_job_service
from app.api.main import create_app
from app.config import Settings
from app.core.models import JobRecord, JobStatus
from app.worker.claim_loop import ClaimLoop

SAMPLES = Path(__file__).parents[2] / "samples"


def _job_record(
    *,
    status: JobStatus = JobStatus.RUNNING,
    error_code: str | None = None,
    error_detail: str = "must never be sent",
) -> JobRecord:
    now = datetime.now(UTC)
    return JobRecord(
        id="job-1",
        document_id="document-1",
        batch_id="batch-1",
        target_language="de",
        status=status,
        total_chunks=1,
        done_chunks=0,
        model="gpt-4o-mini",
        prompt_version="v1",
        tokens_in=0,
        tokens_out=0,
        cost_usd=0.0,
        error_code=error_code,
        error_detail=error_detail,
        idempotency_key="idem-1",
        lease_owner=None,
        lease_expires_at=None,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
async def api_runtime(tmp_path: Path):
    settings = Settings(
        database_path=tmp_path / "api.db",
        upload_storage_path=tmp_path / "uploads",
        output_storage_path=tmp_path / "outputs",
        llm_provider="fake",
        max_chunk_concurrency=2,
    )
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client,
    ):
        yield app, settings, client


async def _upload(client: httpx.AsyncClient, sample: str) -> str:
    path = SAMPLES / sample
    response = await client.post(
        "/api/documents",
        files={"file": (path.name, path.read_bytes())},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["block_count"] > 0
    assert payload["status"] == "analyzing"
    return payload["id"]


async def _run_fake_worker(settings: Settings, job_id: str, client: httpx.AsyncClient) -> dict:
    connection = await SqliteConnectionFactory(settings.database_path).create()
    stop = asyncio.Event()
    try:
        registry = FormatRegistry()
        registry.register("pdf", PdfExtractor(), PdfRenderer())
        registry.register("docx", DocxExtractor(), DocxRenderer())
        registry.register("md", MarkdownExtractor(), MarkdownRenderer())
        loop = ClaimLoop(
            settings,
            SqliteJobExecutionRepository(connection, worker_id=settings.worker_id),
            SqliteTranslationCacheRepository(connection),
            SqliteDocumentRepository(connection),
            FakeProvider(settings=settings),
            ModelCostCalculator(),
            registry,
            FilesystemStorage(settings.upload_storage_path, settings.output_storage_path),
            persistence=WorkerPersistence(connection),
            shutdown_event=stop,
        )
        worker = asyncio.create_task(loop.run())
        try:
            for _ in range(200):
                response = await client.get(f"/api/jobs/{job_id}")
                if response.status_code == 200 and response.json()["status"] in {
                    "done",
                    "completed_with_errors",
                    "failed",
                }:
                    stop.set()
                    await asyncio.wait_for(worker, timeout=3)
                    return response.json()
                await asyncio.sleep(0.025)
            pytest.fail("fake worker did not reach a terminal job state")
        finally:
            if not worker.done():
                stop.set()
                await asyncio.wait_for(worker, timeout=3)
    finally:
        await connection.close()


async def test_upload_create_jobs_idempotency_and_batch(api_runtime) -> None:
    _, _, client = api_runtime
    document_id = await _upload(client, "sample_en.pdf")
    request = {
        "document_id": document_id,
        "target_languages": ["de", "fr"],
        "idempotency_key": "batch-translation-1",
    }

    first = await client.post("/api/jobs", json=request)
    second = await client.post("/api/jobs", json=request)
    assert first.status_code == second.status_code == 200
    first_payload = first.json()
    second_payload = second.json()
    assert len(first_payload["jobs"]) == 2
    assert [job["id"] for job in second_payload["jobs"]] == [
        job["id"] for job in first_payload["jobs"]
    ]
    batch_id = first_payload["batch_id"]
    assert all(job["batch_id"] == batch_id for job in first_payload["jobs"])

    batch = await client.get(f"/api/batches/{batch_id}")
    assert batch.status_code == 200
    assert {job["target_language"] for job in batch.json()["jobs"]} == {"de", "fr"}
    for job in first_payload["jobs"]:
        status = await client.get(f"/api/jobs/{job['id']}")
        assert status.status_code == 200
        assert status.json()["status"] == "queued"
        assert status.json()["total_chunks"] > 0


async def test_recent_jobs_returns_empty_collection(api_runtime) -> None:
    _, _, client = api_runtime
    response = await client.get("/api/jobs")
    assert response.status_code == 200
    assert response.json() == []


async def test_recent_jobs_returns_bounded_safe_summaries(api_runtime) -> None:
    _, _, client = api_runtime
    document_id = await _upload(client, "sample_en.pdf")
    created = await client.post(
        "/api/jobs",
        json={
            "document_id": document_id,
            "target_languages": [f"language-{index}" for index in range(12)],
            "idempotency_key": "recent-api-jobs",
        },
    )
    assert created.status_code == 200
    jobs = created.json()["jobs"]
    default_response = await client.get("/api/jobs")
    assert default_response.status_code == 200
    assert default_response.json() == list(reversed(jobs))[:10]
    assert set(default_response.json()[0]) == {
        "id",
        "document_id",
        "batch_id",
        "target_language",
        "status",
        "total_chunks",
        "done_chunks",
        "cache_hit_blocks",
        "cache_miss_blocks",
        "cost_usd",
        "error",
    }
    for limit, expected in [(2, 2), (0, 1), (-1, 1), (101, 12)]:
        response = await client.get("/api/jobs", params={"limit": limit})
        assert response.status_code == 200
        assert response.json() == list(reversed(jobs))[:expected]


async def test_recent_jobs_invalid_limit_uses_error_catalog(api_runtime) -> None:
    _, _, client = api_runtime
    response = await client.get("/api/jobs", params={"limit": "private-invalid-value"})
    assert response.status_code == 422
    assert response.json() == {
        "error_code": "invalid_request",
        "message": "Request validation failed",
        "retryable": False,
    }


async def test_cache_counts_are_shared_by_rest_sse_and_durable_metrics(api_runtime) -> None:
    _, settings, client = api_runtime
    document_id = await _upload(client, "sample_en.docx")
    completed: list[dict] = []
    for index in range(2):
        created = await client.post(
            "/api/jobs",
            json={
                "document_id": document_id,
                "target_languages": ["de"],
                "idempotency_key": f"cache-observation-{index}",
            },
        )
        assert created.status_code == 200
        queued = created.json()["jobs"][0]
        assert (queued["cache_hit_blocks"], queued["cache_miss_blocks"]) == (0, 0)
        completed.append(await _run_fake_worker(settings, queued["id"], client))

    assert completed[0]["cache_miss_blocks"] > 0
    assert completed[1]["cache_hit_blocks"] > 0
    for job in completed:
        event_response = await client.get(f"/api/jobs/{job['id']}/events")
        data_line = next(
            line[6:] for line in event_response.text.splitlines() if line.startswith("data: ")
        )
        event = json.loads(data_line)
        assert (event["cache_hit_blocks"], event["cache_miss_blocks"]) == (
            job["cache_hit_blocks"],
            job["cache_miss_blocks"],
        )

    expected = {
        "cache_hits_total": sum(job["cache_hit_blocks"] for job in completed),
        "cache_misses_total": sum(job["cache_miss_blocks"] for job in completed),
    }

    def reported_counts(body: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for name in expected:
            value = next(
                line.split()[-1] for line in body.splitlines() if line.startswith(f"{name} ")
            )
            counts[name] = int(float(value))
        return counts

    assert reported_counts((await client.get("/metrics")).text) == expected
    assert reported_counts((await client.get("/metrics")).text) == expected
    restarted = create_app(settings)
    async with (
        restarted.router.lifespan_context(restarted),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restarted), base_url="http://test"
        ) as restarted_client,
    ):
        assert reported_counts((await restarted_client.get("/metrics")).text) == expected


@pytest.mark.parametrize(
    ("sample", "expected_signature"),
    [("sample_en.docx", b"PK\x03\x04"), ("sample_en.pdf", b"%PDF-"), ("sample_en.md", b"# ")],
)
async def test_fake_worker_terminal_sse_and_download(
    api_runtime,
    sample: str,
    expected_signature: bytes,
    monkeypatch,
) -> None:
    _, settings, client = api_runtime
    document_id = await _upload(client, sample)
    created = await client.post(
        "/api/jobs",
        json={
            "document_id": document_id,
            "target_languages": ["de"],
            "idempotency_key": "download-docx-1",
        },
    )
    assert created.status_code == 200
    job_id = created.json()["jobs"][0]["id"]

    terminal = await _run_fake_worker(settings, job_id, client)
    assert terminal["status"] == "done"
    assert terminal["done_chunks"] == terminal["total_chunks"]

    closed: list[Connection] = []
    original_close = Connection.close

    async def tracked_close(connection: Connection) -> None:
        closed.append(connection)
        await original_close(connection)

    monkeypatch.setattr(Connection, "close", tracked_close)
    closed_before_events = len(closed)
    events = await client.get(f"/api/jobs/{job_id}/events")
    assert len(closed) == closed_before_events + 1
    assert events.status_code == 200
    assert events.headers["content-type"].startswith("text/event-stream")
    assert "event: done" in events.text
    data_line = next(line[6:] for line in events.text.splitlines() if line.startswith("data: "))
    assert json.loads(data_line)["status"] == "done"
    assert json.loads(data_line)["cache_hit_blocks"] == terminal["cache_hit_blocks"]
    assert json.loads(data_line)["cache_miss_blocks"] == terminal["cache_miss_blocks"]

    download = await client.get(f"/api/jobs/{job_id}/download")
    assert download.status_code == 200
    assert download.content.startswith(expected_signature)
    assert "attachment" in download.headers.get("content-disposition", "")
    if sample.endswith(".md"):
        assert download.headers["content-type"].startswith("text/markdown")

    partial = await client.get(f"/api/jobs/{job_id}/download", headers={"Range": "bytes=0-3"})
    assert partial.status_code == 206
    assert partial.content == download.content[:4]
    for range_header, status in (("items=0-1", 400), ("bytes=999999999-", 416)):
        invalid = await client.get(f"/api/jobs/{job_id}/download", headers={"Range": range_header})
        assert invalid.status_code == status
        assert invalid.json() == {
            "error_code": "invalid_request",
            "message": "Request validation failed",
            "retryable": False,
        }
        if status == 416:
            assert invalid.headers["content-range"] == f"bytes */{len(download.content)}"

    first_metrics = (await client.get("/metrics")).text
    second_metrics = (await client.get("/metrics")).text
    for metric in ("llm_cost_usd_total", "llm_errors_total"):
        first_value = next(line for line in first_metrics.splitlines() if line.startswith(metric))
        second_value = next(line for line in second_metrics.splitlines() if line.startswith(metric))
        assert first_value == second_value


async def test_retry_conflict_and_structured_not_found(api_runtime) -> None:
    _, _, client = api_runtime
    missing = await client.get("/api/jobs/missing-job")
    assert missing.status_code == 404
    assert missing.json()["error_code"] == "not_found"
    assert "traceback" not in missing.text.lower()

    document_id = await _upload(client, "sample_en.pdf")
    created = await client.post(
        "/api/jobs",
        json={
            "document_id": document_id,
            "target_languages": ["de"],
            "idempotency_key": "retry-active-1",
        },
    )
    job_id = created.json()["jobs"][0]["id"]
    retry = await client.post(f"/api/jobs/{job_id}/retry", json={})
    assert retry.status_code == 409
    assert retry.json()["error_code"] == "conflict"
    assert "error_detail" not in retry.text

    download = await client.get(f"/api/jobs/{job_id}/download")
    assert download.status_code == 409
    assert download.json()["error_code"] == "conflict"


async def test_validation_and_unsupported_upload_errors_are_structured(api_runtime) -> None:
    _, _, client = api_runtime
    validation = await client.post(
        "/api/jobs",
        json={"document_id": "x", "target_languages": [], "idempotency_key": "k"},
    )
    assert validation.status_code == 422
    assert validation.json()["error_code"] == "invalid_request"
    assert "traceback" not in validation.text.lower()

    unsupported = await client.post(
        "/api/documents",
        files={"file": ("notes.txt", b"plain text")},
    )
    assert unsupported.status_code == 415
    assert unsupported.json()["error_code"] == "unsupported_format"

    corrupt = await client.post(
        "/api/documents",
        files={"file": ("corrupt.pdf", b"%PDF-1.7\nthis is not a PDF")},
    )
    assert corrupt.status_code == 422
    assert corrupt.json()["error_code"] == "corrupt_file"

    scanned = fitz.open()
    scanned.new_page().draw_rect(fitz.Rect(20, 20, 80, 80), color=(0, 0, 0), fill=(0, 0, 0))
    scanned_bytes = scanned.tobytes()
    scanned.close()
    scanned_response = await client.post(
        "/api/documents",
        files={"file": ("scanned.pdf", scanned_bytes)},
    )
    assert scanned_response.status_code == 422
    assert scanned_response.json()["error_code"] == "scanned_pdf"


async def test_readiness_metrics_and_safe_internal_errors(api_runtime, monkeypatch) -> None:
    app, _, client = api_runtime
    assert (await client.get("/healthz")).status_code == 200
    ready = await client.get("/readyz")
    assert ready.status_code == 200

    first_metrics = await client.get("/metrics")
    second_metrics = await client.get("/metrics")
    assert first_metrics.status_code == second_metrics.status_code == 200
    for metric in (
        "jobs_by_status",
        "llm_cost_usd_total",
        "llm_errors_total",
        "cache_hits_total",
        "cache_misses_total",
    ):
        first_value = [line for line in first_metrics.text.splitlines() if line.startswith(metric)]
        second_value = [
            line for line in second_metrics.text.splitlines() if line.startswith(metric)
        ]
        assert first_value == second_value

    async def fail_storage_probe(*_paths: Path) -> None:
        raise OSError("private storage path detail")

    monkeypatch.setattr("app.api.dependencies.check_storage_writable", fail_storage_probe)
    not_ready = await client.get("/readyz")
    assert not_ready.status_code == 503
    assert not_ready.json()["error_code"] == "not_ready"
    assert "private storage" not in not_ready.text

    class BrokenService:
        async def get_job(self, _job_id: str) -> None:
            raise RuntimeError("sensitive failure detail")

    app.dependency_overrides[get_job_service] = lambda: BrokenService()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as error_client:
            failed = await error_client.get("/api/jobs/whatever")
    finally:
        app.dependency_overrides.pop(get_job_service, None)
    assert failed.status_code == 500
    assert failed.json()["error_code"] == "internal_error"
    assert "sensitive failure detail" not in failed.text


async def test_upload_limit_is_checked_before_document_service(api_runtime, monkeypatch) -> None:
    _, _, client = api_runtime
    monkeypatch.setattr("app.api.routers.documents.MAX_UPLOAD_BYTES", 4)
    response = await client.post(
        "/api/documents",
        files={"file": ("too-large.pdf", b"12345")},
    )
    assert response.status_code == 413
    assert response.json()["error_code"] == "size_limit"


async def test_request_connection_closes_after_response(tmp_path: Path, monkeypatch) -> None:
    settings = Settings(
        database_path=tmp_path / "connections.db",
        upload_storage_path=tmp_path / "connections-uploads",
        output_storage_path=tmp_path / "connections-outputs",
        llm_provider="fake",
    )
    app = create_app(settings)
    closed: list[Connection] = []
    original_close = Connection.close

    async def tracked_close(connection: Connection) -> None:
        closed.append(connection)
        await original_close(connection)

    monkeypatch.setattr(Connection, "close", tracked_close)
    async with app.router.lifespan_context(app):
        closed.clear()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get("/api/jobs/missing")
        assert response.status_code == 404
        assert len(closed) == 1


async def test_failed_job_can_retry_with_a_raised_cap(api_runtime) -> None:
    _, settings, client = api_runtime
    settings.fake_fail_rate = 1.0
    settings.max_chunk_attempts = 1
    document_id = await _upload(client, "sample_en.pdf")
    created = await client.post(
        "/api/jobs",
        json={
            "document_id": document_id,
            "target_languages": ["de"],
            "idempotency_key": "retry-raised-cap-1",
        },
    )
    assert created.status_code == 200
    job_id = created.json()["jobs"][0]["id"]
    failed = await _run_fake_worker(settings, job_id, client)
    assert failed["status"] == "completed_with_errors"
    failed_errors = (await client.get("/metrics")).text
    failed_count = next(
        line for line in failed_errors.splitlines() if line.startswith("llm_errors_total")
    )

    retry = await client.post(
        f"/api/jobs/{job_id}/retry",
        json={"raised_cost_cap_usd": 3.0},
    )
    assert retry.status_code == 200
    assert retry.json()["status"] == "queued"
    assert retry.json()["error"] is None

    settings.fake_fail_rate = 0.0
    completed = await _run_fake_worker(settings, job_id, client)
    assert completed["status"] == "done"
    error_metrics = (await client.get("/metrics")).text
    error_count = next(
        line for line in error_metrics.splitlines() if line.startswith("llm_errors_total")
    )
    assert failed_count == error_count


async def test_sse_generator_stops_after_disconnect() -> None:
    from app.api.routers.jobs import event_stream

    job = _job_record()

    class FakeService:
        async def get_job(self, _job_id: str) -> JobRecord:
            return job

    class FakeRequest:
        checks = 0

        async def is_disconnected(self) -> bool:
            self.checks += 1
            return True

    request = FakeRequest()
    stream = event_stream(request, FakeService(), job.id)  # type: ignore[arg-type]
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    assert request.checks == 1


async def test_terminal_sse_catalogues_errors_without_leaking_details() -> None:
    from app.api.routers.jobs import event_stream

    job = _job_record(
        status=JobStatus.FAILED,
        error_code="unrecognized_private_code",
        error_detail="provider response must never be sent",
    )

    class FakeService:
        async def get_job(self, _job_id: str) -> JobRecord:
            return job

    class FakeRequest:
        async def is_disconnected(self) -> bool:
            return False

    stream = event_stream(FakeRequest(), FakeService(), job.id)  # type: ignore[arg-type]
    emitted = (await anext(stream)).decode()
    assert "event: error" in emitted
    assert "provider response" not in emitted
    line = next(value[6:] for value in emitted.splitlines() if value.startswith("data: "))
    payload = json.loads(line)
    assert payload["error"] == {
        "error_code": "internal_error",
        "message": "Internal server error",
        "retryable": True,
    }
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
