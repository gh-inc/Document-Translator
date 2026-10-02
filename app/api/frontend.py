"""Serve the built SPA through framework 404 handling, preserving API errors."""

import asyncio
import os
import stat
from pathlib import Path

from fastapi import FastAPI, Request
from starlette.exceptions import HTTPException
from starlette.responses import FileResponse, Response

from app.api.errors import http_error_handler


def _frontend_file(directory: Path, path: str) -> tuple[Path, os.stat_result] | None:
    """Resolve a request within the build; run filesystem work in a thread."""
    if "\\" in path or "\x00" in path or ".." in path.split("/") or path.startswith("//"):
        return None
    try:
        root = directory.resolve()
        index = (root / "index.html").resolve()
        if not index.is_relative_to(root):
            return None
        index_stat = index.stat()
        if not stat.S_ISREG(index_stat.st_mode):
            return None

        relative_path = path.lstrip("/")
        candidate = (root / relative_path).resolve()
        if not candidate.is_relative_to(root):
            return None
        try:
            candidate_stat = candidate.stat()
        except (FileNotFoundError, NotADirectoryError):
            candidate_stat = None
        if candidate_stat is not None and stat.S_ISREG(candidate_stat.st_mode):
            return candidate, candidate_stat

        # Static namespaces and file-like URLs never receive the HTML shell.
        if relative_path.split("/", 1)[0] in {"assets", "branding"}:
            return None
        # Batch identifiers are two digests joined by a dot. Match only the
        # SPA's single-ID routes before classifying dotted leaves as files.
        segments = relative_path.split("/")
        if len(segments) == 2 and segments[0] in {"batches", "jobs"} and segments[1]:
            return index, index_stat
        if "." in Path(relative_path).name:
            return None
        return index, index_stat
    except (OSError, ValueError, RuntimeError):
        return None


def register_frontend_handler(app: FastAPI, directory: Path) -> None:
    async def frontend_not_found(request: Request, exc: HTTPException) -> Response:
        path = request.url.path
        if request.method not in {"GET", "HEAD"} or path == "/api" or path.startswith("/api/"):
            return await http_error_handler(request, exc)
        file = await asyncio.to_thread(_frontend_file, directory, path)
        if file is None:
            return await http_error_handler(request, exc)
        file_path, file_stat = file
        return FileResponse(file_path, stat_result=file_stat)

    # Starlette selects status handlers before exception-class handlers; all
    # non-404 framework errors and catalogued service failures retain theirs.
    app.add_exception_handler(404, frontend_not_found)  # type: ignore[arg-type]
