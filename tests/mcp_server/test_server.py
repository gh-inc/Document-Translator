import asyncio
import os
from pathlib import Path

import pymupdf
import pytest
from aiosqlite import Connection
from fastmcp import Client
from structlog.testing import capture_logs

from app.adapters.llm.fake_triage_agent import FakeTriageAgent
from app.adapters.storage.document_locks import release_document_lock, try_document_lock
from app.config import Settings
from app.mcp_server.runtime import McpRuntime
from app.mcp_server.server import create_server
from app.worker.__main__ import run_worker


async def test_protocol_exposes_approved_tools_and_structured_errors(
    mcp_settings: Settings,
) -> None:
    async with Client(create_server(mcp_settings)) as client:
        tools = await client.list_tools()
        assert {tool.name for tool in tools} == {
            "translate_file",
            "check_status",
            "download_result",
            "list_recent_jobs",
        }
        assert all(tool.output_schema is not None for tool in tools)
        result = await client.call_tool("check_status", {"job_id": "missing"})
        assert result.structured_content is not None
        assert result.structured_content["result"]["error_code"] == "not_found"
        assert result.structured_content["result"]["retryable"] is False
        recent = await client.call_tool("list_recent_jobs", {})
        assert recent.data == []
        invalid = await client.call_tool(
            "translate_file", {"path": "missing.pdf", "target_languages": []}
        )
        assert invalid.structured_content["result"]["error_code"] == "invalid_request"


async def test_mcp_pdf_worker_end_to_end(runtime: McpRuntime, input_pdf: Path) -> None:
    async with Client(create_server(runtime=runtime)) as client:
        submitted = await client.call_tool(
            "translate_file", {"path": "input/sample.pdf", "target_languages": ["de"]}
        )
        payload = submitted.structured_content["result"]
        assert payload is not None and len(payload["job_ids"]) == 1
        job_id = payload["job_ids"][0]
        shutdown = asyncio.Event()
        worker = asyncio.create_task(run_worker(runtime.settings, shutdown_event=shutdown))
        try:
            async with asyncio.timeout(10):
                while True:
                    result = await client.call_tool("check_status", {"job_id": job_id})
                    status = result.structured_content["result"]
                    assert status is not None
                    if status["status"] == "done":
                        break
                    assert status["status"] not in {"failed", "completed_with_errors"}
                    await asyncio.sleep(0.02)
            assert status["done_chunks"] == status["total_chunks"] > 0
            downloaded = await client.call_tool(
                "download_result", {"job_id": job_id, "output_dir": "output"}
            )
            saved = Path(downloaded.structured_content["result"]["path"])
            assert saved.is_relative_to(runtime.settings.mcp_shared_dir)
            with pymupdf.open(saved) as output:
                text = "".join(page.get_text() for page in output)
            assert "[de]" in text and "Hello World" in text
            recent = await client.call_tool("list_recent_jobs", {"limit": 1})
            assert recent.structured_content["result"][0]["id"] == job_id
        finally:
            shutdown.set()
            await asyncio.wait_for(worker, timeout=3)


async def test_shutdown_cancels_triage_and_closes_agent(
    runtime: McpRuntime, input_pdf: Path
) -> None:
    entered = asyncio.Event()
    closed = asyncio.Event()

    class SlowAgent(FakeTriageAgent):
        async def analyze(self, document):
            entered.set()
            await asyncio.Event().wait()

        async def aclose(self) -> None:
            closed.set()

    runtime.settings.mcp_triage_timeout_seconds = 0.03
    runtime.agent_factory = lambda settings: SlowAgent(settings=settings)
    async with Client(create_server(runtime=runtime)) as client:
        result = await client.call_tool(
            "translate_file", {"path": str(input_pdf), "target_languages": ["de"]}
        )
        assert result.structured_content["result"]["error_code"] == "analysis_pending"
        await asyncio.wait_for(entered.wait(), timeout=1)
    assert closed.is_set()
    assert not runtime._triage_tasks


async def test_startup_reports_an_unusable_shared_output_directory(
    mcp_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = McpRuntime(mcp_settings)
    # Probe by intercepting the access check rather than chmod-ing a fixture: a
    # permission test based on file modes passes silently when the suite runs as
    # root, which is exactly where this defect would stay invisible.
    monkeypatch.setattr(os, "access", lambda *_args, **_kwargs: False)
    try:
        with capture_logs() as logs:
            await runtime.startup()
    finally:
        await runtime.aclose()

    reports = [log for log in logs if log["event"] == "mcp_shared_dir_not_writable"]
    assert reports, "startup did not report an unusable shared output directory"
    report = reports[0]
    assert report["log_level"] == "error"
    assert report["uid"] == os.getuid()
    assert report["directory_name"] == "output"
    assert report["remedy"]
    # No resolved host path may reach the log.
    assert str(mcp_settings.mcp_shared_dir) not in str(logs)


async def test_startup_stays_silent_when_the_shared_output_is_usable(
    runtime: McpRuntime,
) -> None:
    with capture_logs() as logs:
        await runtime.startup()
    assert not [log for log in logs if log["event"] == "mcp_shared_dir_not_writable"]


async def test_shutdown_releases_claim_cancelled_before_run_starts(
    runtime: McpRuntime, input_pdf: Path
) -> None:
    content = await asyncio.to_thread(input_pdf.read_bytes)
    async with runtime.services() as services:
        result = await services.documents.upload(input_pdf.name, content)
        document = result.document
    await runtime.schedule_triage(document.id)
    # schedule_triage returns immediately after task creation. Cancel before
    # yielding control, so ClaimedTriage.run's finally block cannot execute.
    await runtime.aclose()
    descriptor = await try_document_lock(runtime.settings.database_path, document.id, "triage")
    assert descriptor is not None
    await release_document_lock(descriptor)


async def test_mcp_connections_close_before_poll_sleeps_and_provider_calls(
    runtime: McpRuntime, input_pdf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.adapters.persistence.database import SqliteConnectionFactory
    from app.core.models import DocumentIR, TriageResult
    from app.mcp_server.server import McpTools

    active_runtime_connections: set[Connection] = set()
    active_connections: set[Connection] = set()
    original_create = SqliteConnectionFactory.create
    original_close = Connection.close
    original_sleep = asyncio.sleep
    slept = asyncio.Event()
    analyzed = asyncio.Event()

    async def create(factory: SqliteConnectionFactory) -> Connection:
        connection = await original_create(factory)
        active_connections.add(connection)
        if factory is runtime.factory:
            active_runtime_connections.add(connection)
        return connection

    async def close(connection: Connection) -> None:
        await original_close(connection)
        active_connections.discard(connection)
        active_runtime_connections.discard(connection)

    async def sleep(delay: float, result=None):
        # Other triage repository scopes can run concurrently with this sleep;
        # the polling tool's own connections must already have closed.
        assert not active_runtime_connections
        slept.set()
        return await original_sleep(delay, result)

    class ObservedAgent(FakeTriageAgent):
        async def analyze(self, document: DocumentIR) -> TriageResult:
            # Independent status polls may run at the same time. The triage
            # repository's own scopes must close before invoking the provider.
            assert active_connections.issubset(active_runtime_connections)
            analyzed.set()
            await original_sleep(0.05)
            return await super().analyze(document)

    monkeypatch.setattr(SqliteConnectionFactory, "create", create)
    monkeypatch.setattr(Connection, "close", close)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    runtime.agent_factory = lambda settings: ObservedAgent(settings=settings)
    result = await McpTools(runtime).translate_file(str(input_pdf), ["de"])
    assert result.model_dump().get("job_ids")
    assert slept.is_set() and analyzed.is_set()
    assert not active_connections


def test_entrypoint_uses_streamable_http(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.mcp_server import __main__

    calls = []

    class StubServer:
        def run(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(__main__, "create_server", StubServer)
    __main__.main()
    assert calls == [
        {"transport": "streamable-http", "host": "0.0.0.0", "port": 8001, "path": "/mcp"}
    ]
