import asyncio
import os
from pathlib import Path

import pytest

from app.adapters.llm.fake_triage_agent import FakeTriageAgent
from app.adapters.persistence.database import transaction
from app.core.errors import ErrorCode
from app.core.models import DocumentStatus, TriageStatus
from app.mcp_server.runtime import McpRuntime
from app.mcp_server.schemas import DocumentStatusResult, ToolError, TranslationSubmission
from app.mcp_server.server import McpTools, _read_input


async def test_translate_enqueues_after_analysis_and_reuses_duplicate(
    runtime: McpRuntime, input_pdf: Path
) -> None:
    tools = McpTools(runtime)
    first = await tools.translate_file("input/sample.pdf", ["DE", "fr", "de"])
    assert isinstance(first, TranslationSubmission)
    assert len(first.job_ids) == 2
    document = await runtime.get_document(first.document_id)
    assert document is not None and document.status is DocumentStatus.EXTRACTED
    second = await tools.translate_file(str(input_pdf), ["de", "fr"])
    assert second == first
    async with runtime.services() as services:
        jobs = await services.jobs.list_recent_jobs()
    assert len(jobs) == 2
    assert {job.target_language for job in jobs} == {"de", "fr"}


async def test_failed_agent_uses_degraded_plan(runtime: McpRuntime, input_pdf: Path) -> None:
    runtime.agent_factory = lambda settings: FakeTriageAgent(settings=settings, fail_rate=1)
    submission = await McpTools(runtime).translate_file(str(input_pdf), ["de"])
    assert isinstance(submission, TranslationSubmission)
    async with runtime.services() as services:
        analysis = await services.document_repo.get_analysis(submission.document_id)
    assert analysis is not None and analysis.triage_status is TriageStatus.DEGRADED


async def test_deadline_has_document_recovery_and_analysis_continues(
    runtime: McpRuntime, input_pdf: Path
) -> None:
    runtime.settings.mcp_triage_timeout_seconds = 0.03
    runtime.agent_factory = lambda settings: FakeTriageAgent(settings=settings, latency_ms=200)
    tools = McpTools(runtime)
    result = await tools.translate_file(str(input_pdf), ["de"])
    assert isinstance(result, ToolError)
    assert result.error_code is ErrorCode.ANALYSIS_PENDING and result.retryable
    assert result.document_id is not None
    assert "check_status" in result.message
    status = await tools.check_status(result.document_id)
    assert isinstance(status, DocumentStatusResult)
    assert status.status is DocumentStatus.ANALYZING
    assert await tools.list_recent_jobs() == []
    async with asyncio.timeout(3):
        while True:
            document = await runtime.get_document(result.document_id)
            if document is not None and document.status is DocumentStatus.EXTRACTED:
                break
            await asyncio.sleep(0.01)
    status = await tools.check_status(result.document_id)
    assert isinstance(status, DocumentStatusResult)
    assert status.status is DocumentStatus.EXTRACTED
    assert "translate_file" in status.next_action
    assert isinstance(await tools.translate_file(str(input_pdf), ["de"]), TranslationSubmission)


async def test_slow_poll_is_bounded_by_deadline(
    runtime: McpRuntime, input_pdf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime.settings.mcp_triage_timeout_seconds = 0.03

    async def skip_triage(_document_id: str) -> None:
        return None

    async def slow_poll(_document_id: str) -> None:
        await asyncio.sleep(5)

    monkeypatch.setattr(runtime, "schedule_triage", skip_triage)
    monkeypatch.setattr(runtime, "get_document", slow_poll)
    # A slow repository poll, not just the polling sleep, must be interrupted.
    async with asyncio.timeout(1):
        result = await McpTools(runtime).translate_file(str(input_pdf), ["de"])
    assert isinstance(result, ToolError) and result.error_code is ErrorCode.ANALYSIS_PENDING


async def test_busy_sqlite_claim_respects_deadline_and_analysis_resumes(
    runtime: McpRuntime, input_pdf: Path
) -> None:
    content = await asyncio.to_thread(input_pdf.read_bytes)
    async with runtime.services() as services:
        document, _count = await services.documents.upload(input_pdf.name, content)
    runtime.settings.mcp_triage_timeout_seconds = 0.04
    writer = await runtime.factory.create()
    try:
        async with transaction(writer):
            # This independent writer blocks triage's BEGIN IMMEDIATE. The
            # initial claim keeps running after the client deadline expires.
            async with asyncio.timeout(0.25):
                result = await McpTools(runtime).translate_file(str(input_pdf), ["de"])
            assert isinstance(result, ToolError)
            assert result.error_code is ErrorCode.ANALYSIS_PENDING
            assert result.document_id == document.id
            assert runtime._readiness_tasks  # Work ownership remains tracked.
    finally:
        await writer.close()
    async with asyncio.timeout(2):
        await asyncio.gather(*runtime._readiness_tasks, return_exceptions=True)
    ready = await runtime.get_document(document.id)
    assert ready is not None and ready.status is DocumentStatus.EXTRACTED


async def test_failed_document_returns_safe_stored_error(
    runtime: McpRuntime, input_pdf: Path
) -> None:
    async with runtime.services() as services:
        content = await asyncio.to_thread(input_pdf.read_bytes)
        document, _count = await services.documents.upload(input_pdf.name, content)
        # Arrange through the adapter's transaction, rather than introducing SQL in tools.
        async with services.documents._transaction_context():
            await services.document_repo.update_document_status(
                document.id, DocumentStatus.FAILED, ErrorCode.CORRUPT_FILE
            )
    result = await McpTools(runtime).translate_file(str(input_pdf), ["de"])
    assert isinstance(result, ToolError) and result.error_code is ErrorCode.CORRUPT_FILE
    assert await McpTools(runtime).list_recent_jobs() == []


@pytest.mark.parametrize("languages", [[], [" "], [1]])
async def test_invalid_languages_rejected_before_file_or_upload(
    runtime: McpRuntime, languages: list[str]
) -> None:
    result = await McpTools(runtime).translate_file("does-not-exist.pdf", languages)
    assert isinstance(result, ToolError) and result.error_code is ErrorCode.INVALID_REQUEST
    assert result.document_id is None
    assert not list(runtime.settings.upload_storage_path.iterdir())


async def test_unsupported_format_and_oversized_input(runtime: McpRuntime) -> None:
    root = runtime.settings.mcp_shared_dir
    text = root / "unsupported.txt"
    text.write_text("not a supported document")
    result = await McpTools(runtime).translate_file(str(text), ["de"])
    assert isinstance(result, ToolError) and result.error_code is ErrorCode.UNSUPPORTED_FORMAT
    large = root / "large.pdf"
    with large.open("wb") as stream:
        stream.truncate(50 * 1024 * 1024 + 1)
    result = await McpTools(runtime).translate_file(str(large), ["de"])
    assert isinstance(result, ToolError) and result.error_code is ErrorCode.FILE_TOO_LARGE


async def test_path_escape_is_safe(runtime: McpRuntime, input_pdf: Path) -> None:
    root = runtime.settings.mcp_shared_dir
    outside = root.parent / "outside.pdf"
    outside.write_bytes(await asyncio.to_thread(input_pdf.read_bytes))
    (root / "escape.pdf").symlink_to(outside)
    for path in ("../outside.pdf", "escape.pdf", str(outside)):
        result = await McpTools(runtime).translate_file(path, ["de"])
        assert isinstance(result, ToolError)
        assert result.error_code is ErrorCode.INVALID_REQUEST
        assert str(outside) not in result.message


def test_input_symlink_swap_after_validation_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.mcp_server import server

    root = tmp_path / "shared"
    root.mkdir()
    path = root / "sample.pdf"
    path.write_bytes(b"allowed content")
    outside = tmp_path / "private.pdf"
    outside.write_bytes(b"private content")
    original_resolve = server.require_shared_file

    def validate_then_swap(configured_root: Path, supplied_path: str) -> Path:
        validated = original_resolve(configured_root, supplied_path)
        validated.unlink()
        validated.symlink_to(outside)
        return validated

    monkeypatch.setattr(server, "require_shared_file", validate_then_swap)
    with pytest.raises(OSError):
        _read_input(root, "sample.pdf")
    assert outside.read_bytes() == b"private content"


def test_input_fifo_swap_after_validation_does_not_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.mcp_server import server

    root = tmp_path / "shared"
    root.mkdir()
    path = root / "sample.pdf"
    path.write_bytes(b"allowed content")
    original_resolve = server.require_shared_file

    def validate_then_swap(configured_root: Path, supplied_path: str) -> Path:
        validated = original_resolve(configured_root, supplied_path)
        validated.unlink()
        os.mkfifo(validated)
        return validated

    monkeypatch.setattr(server, "require_shared_file", validate_then_swap)
    with pytest.raises(ValueError, match="regular file"):
        _read_input(root, "sample.pdf")
