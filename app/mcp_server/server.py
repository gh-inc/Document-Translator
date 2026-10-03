"""Four thin FastMCP tools over the shared core services."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import shutil
import stat
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastmcp import FastMCP

from app.config import Settings
from app.core.errors import DocumentError, ErrorCode, ProviderError, ServiceError
from app.core.models import DocumentStatus, JobStatus
from app.core.services.document_service import MAX_UPLOAD_BYTES, sanitize_filename
from app.core.services.job_service import JobService
from app.mcp_server.paths import (
    open_shared_directory,
    require_shared_file,
)
from app.mcp_server.runtime import McpRuntime
from app.mcp_server.schemas import (
    DocumentStatusResult,
    DownloadResult,
    JobSummary,
    ToolError,
    TranslationSubmission,
    job_summary,
    safe_error,
)

_SAFE_EXCEPTIONS = (DocumentError, ProviderError, ServiceError)


class McpTools:
    """Protocol workflow orchestration without SQL or translation logic."""

    def __init__(self, runtime: McpRuntime) -> None:
        self.runtime = runtime

    async def translate_file(
        self, path: str, target_languages: list[str]
    ) -> TranslationSubmission | ToolError:
        """Translate a PDF/DOCX under the shared mount, returning enqueued job IDs.

        Relative paths use MCP_SHARED_DIR. If analysis remains pending, use the
        returned document_id with check_status, then submit this file again.
        """
        document_id: str | None = None
        try:
            languages = JobService._normalize_languages(target_languages)
            root = self.runtime.settings.mcp_shared_dir
            input_path, content = await asyncio.to_thread(_read_input, root, path)
            async with self.runtime.services() as services:
                result = await services.documents.upload(input_path.name, content)
                document = result.document
            document_id = document.id
            readiness = self.runtime.start_readiness(document)
            try:
                done, _pending = await asyncio.wait(
                    {readiness}, timeout=self.runtime.settings.mcp_triage_timeout_seconds
                )
            except BaseException:
                readiness.cancel()
                raise
            if not done:
                # Keep ownership alive when SQLite delays the initial claim:
                # cancelling here could leave an analyzing document without a
                # scheduled agent. Runtime tracks work and drains it on shutdown.
                error = safe_error(ErrorCode.ANALYSIS_PENDING, document_id=document_id)
                error.message = (
                    "Document analysis is pending; call check_status with the document_id later. "
                    "When extracted, submit translate_file again to create translation jobs."
                )
                return error
            document = readiness.result()
            if document.status is DocumentStatus.FAILED:
                return safe_error(
                    document.error_code or ErrorCode.INTERNAL_ERROR, document_id=document_id
                )
            request_key = hashlib.sha256(
                json.dumps([document_id, sorted(languages)], separators=(",", ":")).encode()
            ).hexdigest()
            async with self.runtime.services() as services:
                jobs = await services.jobs.create_jobs(document_id, languages, f"mcp:{request_key}")
            if not jobs:
                raise ServiceError(ErrorCode.INTERNAL_ERROR, status_code=500)
            return TranslationSubmission(
                document_id=document_id,
                batch_id=jobs[0].batch_id,
                job_ids=[job.id for job in jobs],
            )
        except _SAFE_EXCEPTIONS as error:
            return safe_error(error.error_code, document_id=document_id)
        except (OSError, ValueError):
            return safe_error(ErrorCode.INVALID_REQUEST, document_id=document_id)
        except Exception:
            return safe_error(ErrorCode.INTERNAL_ERROR, document_id=document_id)

    async def check_status(self, job_id: str) -> JobSummary | DocumentStatusResult | ToolError:
        """Read job progress, cost, and errors; also accepts a pending document_id."""
        try:
            async with self.runtime.services() as services:
                job = await services.jobs.get_job(job_id)
                if job is not None:
                    return job_summary(job)
                document = await services.document_repo.get_document(job_id)
            if document is None:
                return safe_error(ErrorCode.NOT_FOUND)
            if document.status is DocumentStatus.EXTRACTED:
                action = "Call translate_file again to create translation jobs."
            elif document.status is DocumentStatus.FAILED:
                action = "Review the document error before submitting another file."
            else:
                action = "Call check_status with this document_id again later."
            return DocumentStatusResult(
                document_id=document.id,
                status=document.status,
                error=(
                    safe_error(document.error_code, document_id=document.id)
                    if document.error_code is not None
                    else None
                ),
                next_action=action,
            )
        except _SAFE_EXCEPTIONS as error:
            return safe_error(error.error_code)
        except Exception:
            return safe_error(ErrorCode.INTERNAL_ERROR)

    async def download_result(self, job_id: str, output_dir: str) -> DownloadResult | ToolError:
        """Save a completed translation in a directory under the shared mount."""
        try:
            async with self.runtime.services() as services:
                job = await services.jobs.get_job(job_id)
            if job is None:
                return safe_error(ErrorCode.NOT_FOUND)
            if job.status not in {JobStatus.DONE, JobStatus.COMPLETED_WITH_ERRORS}:
                return safe_error(ErrorCode.CONFLICT)
            try:
                source = await self.runtime.storage.get_output_path(job.id)
            except (OSError, ValueError):
                return safe_error(ErrorCode.INTERNAL_ERROR)
            destination = await asyncio.to_thread(
                _copy_output, self.runtime.settings.mcp_shared_dir, source, job.id, output_dir
            )
            return DownloadResult(job_id=job.id, path=str(destination))
        except _SAFE_EXCEPTIONS as error:
            return safe_error(error.error_code)
        except (OSError, ValueError):
            return safe_error(ErrorCode.INVALID_REQUEST)
        except Exception:
            return safe_error(ErrorCode.INTERNAL_ERROR)

    async def list_recent_jobs(self, limit: int = 10) -> list[JobSummary] | ToolError:
        """List the newest translation jobs; limit is bounded to 1 through 100."""
        try:
            async with self.runtime.services() as services:
                return [job_summary(job) for job in await services.jobs.list_recent_jobs(limit)]
        except _SAFE_EXCEPTIONS as error:
            return safe_error(error.error_code)
        except Exception:
            return safe_error(ErrorCode.INTERNAL_ERROR)


def _read_input(root: Path, supplied_path: str) -> tuple[Path, bytes]:
    path = require_shared_file(root, supplied_path)
    with open_shared_directory(root, str(path.parent), create=False) as (directory_fd, _directory):
        descriptor = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd
        )
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("input must be a regular file")
            if metadata.st_size > MAX_UPLOAD_BYTES:
                raise ServiceError(ErrorCode.FILE_TOO_LARGE, status_code=413)
            # Cap+1 catches file growth without first allocating unbounded bytes.
            content = stream.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise ServiceError(ErrorCode.FILE_TOO_LARGE, status_code=413)
    return path, content


def _copy_output(root: Path, source: Path, job_id: str, output_dir: str) -> Path:
    prefix = hashlib.sha256(job_id.encode()).hexdigest()[:16]
    filename = f"{prefix}-{sanitize_filename(source.name)}"
    with open_shared_directory(root, output_dir, create=True) as (directory_fd, directory):
        _validate_destination(directory_fd, filename)
        temporary_name = f".mcp-download-{secrets.token_hex(16)}"
        descriptor = os.open(
            temporary_name,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        try:
            # Never reopen the temporary name: another process could replace it
            # with a symlink after creation. All writes use our original fd.
            with os.fdopen(descriptor, "wb") as output, source.open("rb") as input_stream:
                shutil.copyfileobj(input_stream, output)
                output.flush()
                original = os.fstat(output.fileno())
                current = os.stat(temporary_name, dir_fd=directory_fd, follow_symlinks=False)
                if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != (
                    original.st_dev,
                    original.st_ino,
                ):
                    raise ValueError("download temporary file was replaced")
                _validate_destination(directory_fd, filename)
                # Directory-relative replacement follows neither source nor
                # destination symlinks, and publishes only the complete copy.
                # Keep the temporary private throughout the copy, then make
                # the completed artifact readable by the host user before the
                # atomic publication.
                os.fchmod(output.fileno(), 0o644)
                os.replace(
                    temporary_name,
                    filename,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                )
            return directory / filename
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=directory_fd)


def _validate_destination(directory_fd: int, filename: str) -> None:
    try:
        metadata = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("download destination must be a regular file")


def create_server(
    settings: Settings | None = None, *, runtime: McpRuntime | None = None
) -> FastMCP:
    configured_runtime = runtime or McpRuntime(settings or Settings())

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[dict[str, object]]:
        await configured_runtime.startup()
        try:
            yield {}
        finally:
            await configured_runtime.aclose()

    server = FastMCP("Document Translator", lifespan=lifespan, mask_error_details=True)
    tools = McpTools(configured_runtime)
    server.tool(name="translate_file")(tools.translate_file)
    server.tool(name="check_status")(tools.check_status)
    server.tool(name="download_result")(tools.download_result)
    server.tool(name="list_recent_jobs")(tools.list_recent_jobs)
    return server
