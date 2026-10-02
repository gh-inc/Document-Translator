"""Real WAL/ASGI integration for asynchronous document analysis."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.adapters.llm.triage_runtime import prepare_triage
from app.adapters.persistence.database import SqliteConnectionFactory, transaction
from app.adapters.persistence.repositories import SqliteDocumentRepository
from app.adapters.persistence.triage import TriagePersistence
from app.api.background import run_triage
from app.api.main import create_app
from app.config import Settings
from app.core.models import DocumentIR, DocumentStatus, TranslationPlan, TriageStatus
from app.core.services.triage_service import TriageService

SAMPLE = Path(__file__).parents[2] / "samples" / "sample_en.pdf"


class ControlledAgent:
    def __init__(self, *, fail: bool = False, blocked: bool = False) -> None:
        self.calls = 0
        self.closed = False
        self.fail = fail
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        if not blocked:
            self.release.set()

    async def analyze(self, document: DocumentIR) -> TranslationPlan:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        if self.fail:
            raise RuntimeError("private provider failure")
        return TranslationPlan(source_language="en", domain="technical", register="formal")

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
async def runtime(tmp_path: Path):
    settings = Settings(
        database_path=tmp_path / "triage.db",
        upload_storage_path=tmp_path / "uploads",
        output_storage_path=tmp_path / "out",
        llm_provider="fake",
    )
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield app, settings, client


@asynccontextmanager
async def repository(settings: Settings) -> AsyncIterator[SqliteDocumentRepository]:
    connection = await SqliteConnectionFactory(settings.database_path).create()
    try:
        yield SqliteDocumentRepository(connection)
    finally:
        await connection.close()


async def upload(client: httpx.AsyncClient) -> httpx.Response:
    return await client.post("/api/documents", files={"file": (SAMPLE.name, SAMPLE.read_bytes())})


async def start_raw_upload(
    app: FastAPI, *, send_failure: bool = False
) -> tuple[asyncio.Task, dict]:
    """Observe response transmission while Starlette still awaits BackgroundTasks.

    httpx's ASGITransport awaits background completion before returning to its
    caller, unlike a real network client, so response ordering uses ASGI send.
    """
    request = httpx.Request(
        "POST", "http://test/api/documents", files={"file": (SAMPLE.name, SAMPLE.read_bytes())}
    )
    body = request.read()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/documents",
        "raw_path": b"/api/documents",
        "query_string": b"",
        "root_path": "",
        "headers": [(key.lower(), value) for key, value in request.headers.raw],
        "client": ("127.0.0.1", 1),
        "server": ("test", 80),
    }
    sent = asyncio.Event()
    received = False
    response_parts: list[bytes] = []
    statuses: list[int] = []

    async def receive():
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.Event().wait()

    async def send(message):
        if message["type"] == "http.response.start":
            statuses.append(message["status"])
        if message["type"] == "http.response.body":
            response_parts.append(message.get("body", b""))
            if not message.get("more_body", False):
                sent.set()
                if send_failure:
                    raise RuntimeError("injected response send failure")

    task = asyncio.create_task(app(scope, receive, send))
    try:
        await asyncio.wait_for(sent.wait(), 5)
        assert statuses == [200]
        return task, json.loads(b"".join(response_parts))
    except BaseException:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise


async def test_response_is_sent_before_triage_and_jobs_wait_for_analysis(runtime) -> None:
    app, settings, client = runtime
    agent = ControlledAgent(blocked=True)
    app.state.triage_agent_factory = lambda _settings: agent
    task, payload = await start_raw_upload(app)
    try:
        assert payload["status"] == "analyzing"
        await asyncio.wait_for(agent.started.wait(), 2)
        assert not task.done()
        async with repository(settings) as repo:
            document = await repo.get_document(payload["id"])
            assert document.status is DocumentStatus.ANALYZING
            assert await repo.get_analysis(payload["id"]) is None
        request = {"document_id": payload["id"], "target_languages": ["de"], "idempotency_key": "k"}
        response = await client.post("/api/jobs", json=request)
        assert response.status_code == 409
        assert response.json() == {
            "error_code": "analysis_pending",
            "message": "Document analysis is pending; retry shortly",
            "retryable": True,
        }
        agent.release.set()
        await asyncio.wait_for(task, 3)
        async with repository(settings) as repo:
            assert (await repo.get_document(payload["id"])).status is DocumentStatus.EXTRACTED
            assert (await repo.get_analysis(payload["id"])).domain == "technical"
        assert (await client.post("/api/jobs", json=request)).status_code == 200
        assert agent.closed
    finally:
        agent.release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_three_failures_publish_degraded_plan_and_allow_jobs(runtime) -> None:
    app, settings, client = runtime
    agent = ControlledAgent(fail=True)
    app.state.triage_agent_factory = lambda _settings: agent
    response = await upload(client)
    assert response.status_code == 200
    document_id = response.json()["id"]
    assert agent.calls == 3
    async with repository(settings) as repo:
        assert (await repo.get_document(document_id)).status is DocumentStatus.EXTRACTED
        analysis = await repo.get_analysis(document_id)
        assert analysis.triage_status is TriageStatus.DEGRADED
        assert analysis.terms == []
        assert analysis.warnings
        assert "private" not in analysis.model_dump_json()
    response = await client.post(
        "/api/jobs",
        json={
            "document_id": document_id,
            "target_languages": ["de"],
            "idempotency_key": "degraded",
        },
    )
    assert response.status_code == 200


async def test_explicit_retry_recovers_stuck_record_after_restart(runtime) -> None:
    _app, settings, client = runtime
    document_id = (await upload(client)).json()["id"]
    connection = await SqliteConnectionFactory(settings.database_path).create()
    try:
        async with transaction(connection):
            async with connection.execute(
                "DELETE FROM document_analyses WHERE document_id = ?", (document_id,)
            ):
                pass
            await SqliteDocumentRepository(connection).update_document_status(
                document_id, DocumentStatus.ANALYZING
            )
    finally:
        await connection.close()
    # A new application has no old in-memory task/lock state.
    restarted = create_app(settings)
    async with (
        restarted.router.lifespan_context(restarted),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restarted), base_url="http://test"
        ) as fresh_client,
    ):
        response = await fresh_client.post(f"/api/documents/{document_id}/retry-triage")
        assert response.status_code == 200
        assert response.json()["status"] == "analyzing"
    async with repository(settings) as repo:
        assert (await repo.get_document(document_id)).status is DocumentStatus.EXTRACTED
        assert (await repo.get_analysis(document_id)).triage_status is TriageStatus.OK


async def test_retry_replaces_degraded_analysis_and_success_is_reused(runtime) -> None:
    app, settings, client = runtime
    broken = ControlledAgent(fail=True)
    app.state.triage_agent_factory = lambda _settings: broken
    document_id = (await upload(client)).json()["id"]
    healthy = ControlledAgent()
    app.state.triage_agent_factory = lambda _settings: healthy
    assert (await client.post(f"/api/documents/{document_id}/retry-triage")).json()[
        "status"
    ] == "analyzing"
    async with repository(settings) as repo:
        original = await repo.get_analysis(document_id)
        assert original.triage_status is TriageStatus.OK
        assert original.domain == "technical"
    duplicate = await client.post(
        "/api/documents", files={"file": ("renamed.pdf", SAMPLE.read_bytes())}
    )
    assert duplicate.json()["id"] == document_id
    assert duplicate.json()["status"] == "extracted"
    assert (await client.post(f"/api/documents/{document_id}/retry-triage")).json()[
        "status"
    ] == "extracted"
    assert healthy.calls == 1
    async with repository(settings) as repo:
        assert await repo.get_analysis(document_id) == original
    assert (await client.post("/api/documents/missing/retry-triage")).status_code == 404


async def test_duplicate_scheduled_tasks_serialize_and_skip_second_analysis(runtime) -> None:
    app, settings, client = runtime
    agent = ControlledAgent(blocked=True)
    app.state.triage_agent_factory = lambda _settings: agent
    first, payload = await start_raw_upload(app)
    second = asyncio.create_task(client.post(f"/api/documents/{payload['id']}/retry-triage"))
    try:
        await asyncio.wait_for(agent.started.wait(), 2)
        assert (await client.get("/healthz")).status_code == 200
        agent.release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 5)
        assert agent.calls == 1
    finally:
        agent.release.set()
        await asyncio.gather(first, second, return_exceptions=True)


async def test_missing_analysis_blocks_jobs_even_when_document_is_extracted(runtime) -> None:
    _app, settings, client = runtime
    document_id = (await upload(client)).json()["id"]
    connection = await SqliteConnectionFactory(settings.database_path).create()
    try:
        async with (
            transaction(connection),
            connection.execute(
                "DELETE FROM document_analyses WHERE document_id = ?", (document_id,)
            ),
        ):
            pass
    finally:
        await connection.close()
    response = await client.post(
        "/api/jobs",
        json={"document_id": document_id, "target_languages": ["de"], "idempotency_key": "missing"},
    )
    assert response.status_code == 409
    assert response.json()["error_code"] == "analysis_pending"


async def test_timeout_falls_back_and_cancellation_remains_retryable(runtime) -> None:
    app, settings, client = runtime
    blocking = ControlledAgent(blocked=True)
    app.state.triage_agent_factory = lambda _settings: blocking
    task, payload = await start_raw_upload(app)
    await asyncio.wait_for(blocking.started.wait(), 2)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert blocking.closed
    connection = await SqliteConnectionFactory(settings.database_path).create()
    try:
        service = TriageService(
            SqliteDocumentRepository(connection),
            blocking,
            lambda: transaction(connection),
            TriagePersistence(connection).discard_degraded_analysis,
            attempt_timeout_seconds=0.01,
            retry_delay_seconds=0,
        )
        await asyncio.wait_for(service.run(payload["id"]), 2)
        analysis = await SqliteDocumentRepository(connection).get_analysis(payload["id"])
        assert analysis.triage_status is TriageStatus.DEGRADED
        assert not connection.in_transaction
    finally:
        await connection.close()


async def test_empty_extracted_document_is_failed_with_catalogued_error(runtime) -> None:
    _app, settings, _client = runtime
    connection = await SqliteConnectionFactory(settings.database_path).create()
    try:
        repo = SqliteDocumentRepository(connection)
        async with transaction(connection):
            await repo.create_document("empty", "empty.pdf", "pdf", 1, "/unused")
            await repo.update_document_status("empty", DocumentStatus.ANALYZING)
    finally:
        await connection.close()
    await run_triage("empty", settings, asyncio.Lock(), lambda _: ControlledAgent())
    async with repository(settings) as repo:
        document = await repo.get_document("empty")
        assert document.status is DocumentStatus.FAILED
        assert document.error_code == "corrupt_file"


async def test_concurrent_identical_uploads_share_document_and_analysis(runtime) -> None:
    app, settings, client = runtime
    agent = ControlledAgent()
    app.state.triage_agent_factory = lambda _settings: agent
    first, second = await asyncio.gather(upload(client), upload(client))
    assert first.status_code == second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert agent.calls == 1
    async with repository(settings) as repo:
        assert (await repo.get_document(first.json()["id"])).status is DocumentStatus.EXTRACTED
        assert (await repo.get_analysis(first.json()["id"])).triage_status is TriageStatus.OK


async def test_analysis_and_status_roll_back_if_publication_fails(
    runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, settings, client = runtime
    broken = ControlledAgent(fail=True)
    app.state.triage_agent_factory = lambda _settings: broken
    document_id = (await upload(client)).json()["id"]
    connection = await SqliteConnectionFactory(settings.database_path).create()
    try:
        repo = SqliteDocumentRepository(connection)
        original = await repo.get_analysis(document_id)
        async with transaction(connection):
            await repo.update_document_status(document_id, DocumentStatus.ANALYZING)

        async def fail_save(*_args, **_kwargs):
            raise RuntimeError("injected persistence failure")

        monkeypatch.setattr(repo, "save_analysis", fail_save)
        service = TriageService(
            repo,
            ControlledAgent(),
            lambda: transaction(connection),
            TriagePersistence(connection).discard_degraded_analysis,
        )
        with pytest.raises(RuntimeError, match="injected persistence failure"):
            await service.run(document_id)
        assert await repo.get_analysis(document_id) == original
        assert (await repo.get_document(document_id)).status is DocumentStatus.ANALYZING
        assert not connection.in_transaction
    finally:
        await connection.close()


async def test_degraded_plan_cannot_be_replaced_after_jobs_are_created(runtime) -> None:
    app, settings, client = runtime
    broken = ControlledAgent(fail=True)
    app.state.triage_agent_factory = lambda _settings: broken
    document_id = (await upload(client)).json()["id"]
    response = await client.post(
        "/api/jobs",
        json={"document_id": document_id, "target_languages": ["de"], "idempotency_key": "freeze"},
    )
    assert response.status_code == 200
    async with repository(settings) as repo:
        original = await repo.get_analysis(document_id)
    healthy = ControlledAgent()
    app.state.triage_agent_factory = lambda _settings: healthy
    response = await client.post(f"/api/documents/{document_id}/retry-triage")
    assert response.status_code == 409
    assert response.json()["error_code"] == "conflict"
    assert healthy.calls == 0
    async with repository(settings) as repo:
        assert await repo.get_analysis(document_id) == original
        assert (await repo.get_document(document_id)).status is DocumentStatus.EXTRACTED


async def test_duplicate_upload_still_rejects_mismatched_suffix(runtime) -> None:
    _app, settings, client = runtime
    original = (await upload(client)).json()
    response = await client.post(
        "/api/documents", files={"file": ("wrong.docx", SAMPLE.read_bytes())}
    )
    assert response.status_code == 422
    assert response.json()["error_code"] == "corrupt_file"
    async with repository(settings) as repo:
        assert (await repo.get_document(original["id"])).status is DocumentStatus.EXTRACTED
        assert await repo.get_analysis(original["id"]) is not None
    repeated = await upload(client)
    assert repeated.status_code == 200
    assert repeated.json()["id"] == original["id"]


async def test_response_send_failure_releases_unscheduled_claim(runtime) -> None:
    app, settings, _client = runtime
    agent = ControlledAgent()
    app.state.triage_agent_factory = lambda _: agent
    task, payload = await start_raw_upload(app, send_failure=True)
    with pytest.raises(RuntimeError, match="injected response send failure"):
        await task
    assert agent.calls == 0
    recovered = await prepare_triage(payload["id"], settings, lambda _: agent)
    assert recovered is not None
    await recovered.run()
    assert agent.calls == 1
    async with repository(settings) as repo:
        assert (await repo.get_document(payload["id"])).status is DocumentStatus.EXTRACTED


async def test_independent_app_uploads_keep_original_artifact_and_one_analysis(runtime) -> None:
    app, settings, client = runtime
    agent = ControlledAgent()
    app.state.triage_agent_factory = lambda _: agent
    independent = create_app(settings)
    independent.state.triage_agent_factory = lambda _: agent
    async with (
        independent.router.lifespan_context(independent),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=independent), base_url="http://test"
        ) as second_client,
    ):
        first, second = await asyncio.gather(
            upload(client),
            second_client.post(
                "/api/documents", files={"file": ("renamed.pdf", SAMPLE.read_bytes())}
            ),
        )
    assert first.status_code == second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert agent.calls == 1
    async with repository(settings) as repo:
        document = await repo.get_document(first.json()["id"])
        assert document.status is DocumentStatus.EXTRACTED
        assert (
            await asyncio.to_thread(Path(document.storage_path).read_bytes) == SAMPLE.read_bytes()
        )
