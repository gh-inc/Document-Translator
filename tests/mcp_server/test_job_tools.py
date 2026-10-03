import asyncio
import errno
import hashlib
import os
import shutil
import stat
from pathlib import Path

import pytest

from app.adapters.persistence.database import transaction
from app.core.errors import ErrorCode
from app.core.models import JobStatus
from app.mcp_server.runtime import McpRuntime
from app.mcp_server.schemas import DownloadResult, JobSummary, ToolError, TranslationSubmission
from app.mcp_server.server import McpTools, _copy_output


async def test_missing_jobs_empty_list_and_nonterminal_download(
    runtime: McpRuntime, input_pdf: Path
) -> None:
    tools = McpTools(runtime)
    missing = await tools.check_status("missing")
    assert isinstance(missing, ToolError) and missing.error_code is ErrorCode.NOT_FOUND
    missing_download = await tools.download_result("missing", "output")
    assert isinstance(missing_download, ToolError)
    assert missing_download.error_code is ErrorCode.NOT_FOUND
    assert await tools.list_recent_jobs() == []
    submission = await tools.translate_file(str(input_pdf), ["de", "fr"])
    assert isinstance(submission, TranslationSubmission)
    status = await tools.check_status(submission.job_ids[0])
    assert isinstance(status, JobSummary)
    assert status.status is JobStatus.QUEUED
    assert status.done_chunks == 0 and status.total_chunks > 0 and status.cost_usd == 0
    assert (status.cache_hit_blocks, status.cache_miss_blocks) == (0, 0)
    blocked = await tools.download_result(status.id, "output")
    assert isinstance(blocked, ToolError) and blocked.error_code is ErrorCode.CONFLICT
    assert len(await tools.list_recent_jobs(1)) == 1
    assert len(await tools.list_recent_jobs(0)) == 1
    assert len(await tools.list_recent_jobs(10000)) == 2


async def test_download_reports_a_permission_fault_as_non_retryable(
    runtime: McpRuntime, input_pdf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host-side ownership fault cannot be fixed by any client action."""
    tools = McpTools(runtime)
    submission = await tools.translate_file(str(input_pdf), ["de"])
    assert isinstance(submission, TranslationSubmission)
    job_id = submission.job_ids[0]
    await runtime.storage.save_output(job_id, b"translated bytes", "sample.pdf")
    connection = await runtime.factory.create()
    try:
        from app.adapters.persistence.repositories import SqliteJobExecutionRepository

        repository = SqliteJobExecutionRepository(connection)
        async with transaction(connection):
            await repository.complete_job(job_id, JobStatus.DONE)
    finally:
        await connection.close()

    def _deny(*_args: object, **_kwargs: object) -> Path:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("app.mcp_server.server._copy_output", _deny)
    result = await tools.download_result(job_id, "output")
    assert isinstance(result, ToolError)
    assert result.error_code is ErrorCode.SHARED_DIR_UNAVAILABLE
    assert result.retryable is False
    # The catalogued message must not carry a host path or the raw errno text.
    assert "Permission denied" not in result.message
    assert str(runtime.settings.mcp_shared_dir) not in result.message


async def test_download_keeps_other_filesystem_errors_retryable(
    runtime: McpRuntime, input_pdf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENOSPC and friends stay internal and retryable; only permission is terminal."""
    tools = McpTools(runtime)
    submission = await tools.translate_file(str(input_pdf), ["de"])
    assert isinstance(submission, TranslationSubmission)
    job_id = submission.job_ids[0]
    await runtime.storage.save_output(job_id, b"translated bytes", "sample.pdf")
    connection = await runtime.factory.create()
    try:
        from app.adapters.persistence.repositories import SqliteJobExecutionRepository

        repository = SqliteJobExecutionRepository(connection)
        async with transaction(connection):
            await repository.complete_job(job_id, JobStatus.DONE)
    finally:
        await connection.close()

    def _full(*_args: object, **_kwargs: object) -> Path:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("app.mcp_server.server._copy_output", _full)
    result = await tools.download_result(job_id, "output")
    assert isinstance(result, ToolError)
    assert result.error_code is ErrorCode.INTERNAL_ERROR
    assert result.retryable is True


async def test_download_complete_partial_and_path_guards(
    runtime: McpRuntime, input_pdf: Path
) -> None:
    tools = McpTools(runtime)
    submission = await tools.translate_file(str(input_pdf), ["de"])
    assert isinstance(submission, TranslationSubmission)
    job_id = submission.job_ids[0]
    await runtime.storage.save_output(job_id, b"translated bytes", "sample.pdf")
    connection = await runtime.factory.create()
    try:
        from app.adapters.persistence.repositories import SqliteJobExecutionRepository

        repository = SqliteJobExecutionRepository(connection)
        async with transaction(connection):
            await repository.complete_job(job_id, JobStatus.COMPLETED_WITH_ERRORS)
    finally:
        await connection.close()
    saved = await tools.download_result(job_id, "output/nested")
    assert isinstance(saved, DownloadResult)
    assert await asyncio.to_thread(Path(saved.path).read_bytes) == b"translated bytes"
    assert Path(saved.path).is_relative_to(runtime.settings.mcp_shared_dir)
    outside = runtime.settings.mcp_shared_dir.parent / "outside"
    outside.mkdir()
    (runtime.settings.mcp_shared_dir / "escape").symlink_to(outside, target_is_directory=True)
    for supplied in ("../outside", "escape", str(outside)):
        result = await tools.download_result(job_id, supplied)
        assert isinstance(result, ToolError) and result.error_code is ErrorCode.INVALID_REQUEST
    output = runtime.settings.mcp_shared_dir / "output"
    prefix = hashlib.sha256(job_id.encode()).hexdigest()[:16]
    destination = output / f"{prefix}-sample.pdf"
    outside_file = outside / "victim.pdf"
    outside_file.write_bytes(b"untouched")
    destination.symlink_to(outside_file)
    result = await tools.download_result(job_id, "output")
    assert isinstance(result, ToolError) and result.error_code is ErrorCode.INVALID_REQUEST
    assert outside_file.read_bytes() == b"untouched"


@pytest.mark.parametrize(
    ("failure", "expected_code", "expected_retryable", "expected_message"),
    [
        # Permission is terminal: no client action resolves host ownership.
        (
            PermissionError("private permission detail"),
            ErrorCode.SHARED_DIR_UNAVAILABLE,
            False,
            "Shared translation directory is not writable by the service",
        ),
        # Every other OSError stays internal and retryable.
        (
            OSError(errno.ENOSPC, "private disk detail"),
            ErrorCode.INTERNAL_ERROR,
            True,
            "Internal server error",
        ),
        (
            OSError(errno.EIO, "private io detail"),
            ErrorCode.INTERNAL_ERROR,
            True,
            "Internal server error",
        ),
        (
            ValueError("private validation detail"),
            ErrorCode.INVALID_REQUEST,
            False,
            "Request validation failed",
        ),
    ],
)
async def test_download_maps_filesystem_errors_to_internal_error(
    runtime: McpRuntime,
    input_pdf: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    expected_code: ErrorCode,
    expected_retryable: bool,
    expected_message: str,
) -> None:
    tools = McpTools(runtime)
    submission = await tools.translate_file(str(input_pdf), ["de"])
    assert isinstance(submission, TranslationSubmission)
    job_id = submission.job_ids[0]
    await runtime.storage.save_output(job_id, b"translated bytes", "sample.pdf")
    connection = await runtime.factory.create()
    try:
        from app.adapters.persistence.repositories import SqliteJobExecutionRepository

        repository = SqliteJobExecutionRepository(connection)
        async with transaction(connection):
            await repository.complete_job(job_id, JobStatus.COMPLETED_WITH_ERRORS)
    finally:
        await connection.close()

    def raise_failure(*_args: object, **_kwargs: object) -> Path:
        raise failure

    monkeypatch.setattr("app.mcp_server.server._copy_output", raise_failure)

    result = await tools.download_result(job_id, "output")

    assert isinstance(result, ToolError)
    assert result.error_code is expected_code
    assert result.retryable is expected_retryable
    assert result.message == expected_message
    assert "private" not in result.model_dump_json()


def test_copy_output_treats_job_id_as_data(tmp_path: Path) -> None:
    root = tmp_path / "shared"
    root.mkdir()
    source = tmp_path / "result.pdf"
    source.write_bytes(b"translated")
    result = _copy_output(root, source, "../../outside/arbitrary", "output")
    assert result.parent == root / "output"
    assert result.read_bytes() == b"translated"
    assert stat.S_IMODE(result.stat().st_mode) == 0o644
    assert "/" not in result.name


def test_copy_output_is_private_until_complete_and_published_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "shared"
    root.mkdir()
    source = tmp_path / "result.pdf"
    source.write_bytes(b"translated")
    temporary_modes: list[int] = []
    original_copy = shutil.copyfileobj

    def observe_private_copy(input_stream, output_stream, length=0):
        temporary_modes.append(stat.S_IMODE(os.fstat(output_stream.fileno()).st_mode))
        return original_copy(input_stream, output_stream, length)

    monkeypatch.setattr(shutil, "copyfileobj", observe_private_copy)

    result = _copy_output(root, source, "job", "output")

    assert temporary_modes == [0o600]
    assert stat.S_IMODE(result.stat().st_mode) == 0o644


def test_temporary_symlink_swap_cannot_overwrite_outside_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "shared"
    root.mkdir()
    source = tmp_path / "result.pdf"
    source.write_bytes(b"translated")
    victim = tmp_path / "outside.pdf"
    victim.write_bytes(b"private unchanged")
    created: list[tuple[str, int]] = []
    original_open = os.open
    original_copy = shutil.copyfileobj

    def open_file(path, flags, mode=0o777, *, dir_fd=None):
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        if isinstance(path, str) and path.startswith(".mcp-download-"):
            created.append((path, dir_fd))
        return descriptor

    def swap_before_copy(input_stream, output_stream, length=0):
        filename, directory_fd = created[-1]
        os.unlink(filename, dir_fd=directory_fd)
        os.symlink(str(victim), filename, dir_fd=directory_fd)
        original_copy(input_stream, output_stream, length)

    monkeypatch.setattr(os, "open", open_file)
    monkeypatch.setattr(shutil, "copyfileobj", swap_before_copy)
    with pytest.raises(ValueError, match="temporary file was replaced"):
        _copy_output(root, source, "job", "output")
    assert victim.read_bytes() == b"private unchanged"
    assert not list((root / "output").iterdir())


async def test_stored_unknown_error_never_leaks_diagnostics(
    runtime: McpRuntime, input_pdf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = McpTools(runtime)
    submitted = await tools.translate_file(str(input_pdf), ["de"])
    assert isinstance(submitted, TranslationSubmission)
    async with runtime.services() as services:
        job = await services.jobs.get_job(submitted.job_ids[0])
    assert job is not None
    from app.core.services.job_service import JobService

    async def get_job(_self: JobService, _job_id: str):
        return job.model_copy(
            update={
                "error_code": "secret raw code",
                "error_detail": "private",
                "cache_hit_blocks": 12,
                "cache_miss_blocks": 3,
            }
        )

    monkeypatch.setattr(JobService, "get_job", get_job)
    status = await tools.check_status(job.id)
    assert isinstance(status, JobSummary)
    assert (status.cache_hit_blocks, status.cache_miss_blocks) == (12, 3)
    assert status.error is not None and status.error.error_code is ErrorCode.INTERNAL_ERROR
    assert "private" not in status.model_dump_json()
    assert "secret" not in status.model_dump_json()
