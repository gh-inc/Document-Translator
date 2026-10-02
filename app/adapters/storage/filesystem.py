"""Contained, asynchronous filesystem storage for document artifacts."""

import asyncio
import os
import tempfile
from contextlib import suppress
from pathlib import Path, PureWindowsPath
from threading import RLock

from app.core.ports import FileStorage

_TEMP_PREFIX = ".filesystem-storage-tmp-"


class FilesystemStorage(FileStorage):
    """Store one upload or output artifact beneath a per-record directory.

    A record ID can have exactly one published artifact. Saving it again under
    the same filename atomically replaces it; attempting to publish another
    filename raises ``FileExistsError``. Temporary files are private to the
    write operation and are ignored during artifact discovery.
    """

    def __init__(self, upload_base: Path, output_base: Path) -> None:
        self._upload_base = _canonical_base(upload_base)
        self._output_base = _canonical_base(output_base)
        # The lock makes the one-artifact rule deterministic for concurrent
        # operations through this storage instance.
        self._operation_lock = RLock()

    async def save_upload(
        self,
        document_id: str,
        content: bytes,
        filename: str,
    ) -> Path:
        return await asyncio.to_thread(
            self._save_artifact,
            self._upload_base,
            document_id,
            content,
            filename,
        )

    async def get_upload_path(self, document_id: str) -> Path:
        return await asyncio.to_thread(
            self._get_artifact,
            self._upload_base,
            document_id,
        )

    async def save_output(
        self,
        job_id: str,
        content: bytes,
        filename: str,
    ) -> Path:
        return await asyncio.to_thread(
            self._save_artifact,
            self._output_base,
            job_id,
            content,
            filename,
        )

    async def get_output_path(self, job_id: str) -> Path:
        return await asyncio.to_thread(
            self._get_artifact,
            self._output_base,
            job_id,
        )

    def _save_artifact(
        self,
        base: Path,
        record_id: str,
        content: bytes,
        filename: str,
    ) -> Path:
        _validate_component(record_id, "record ID")
        _validate_component(filename, "filename")
        if filename.startswith(_TEMP_PREFIX):
            raise ValueError("filename uses a reserved storage prefix")

        with self._operation_lock:
            directory = _contained_path(base, record_id)
            if directory.is_symlink():
                raise ValueError("record directory must not be a symlink")
            directory.mkdir(parents=True, exist_ok=True)
            directory = _contained_path(base, record_id)
            if directory.is_symlink() or not directory.is_dir():
                raise ValueError("record path must be a directory inside storage")

            target = _contained_path(base, record_id, filename)
            if target.is_symlink():
                raise ValueError("artifact path must not be a symlink")
            existing = self._published_artifacts(base, directory)
            conflicting = [artifact for artifact in existing if artifact.name != filename]
            if conflicting:
                raise FileExistsError(
                    f"record already has a published artifact: {conflicting[0].name}"
                )
            if target.exists() and not target.is_file():
                raise FileExistsError("artifact path is not a regular file")

            descriptor, temporary_name = tempfile.mkstemp(
                prefix=_TEMP_PREFIX,
                dir=directory,
            )
            temporary_path = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                _assert_contained(base, temporary_path)
                target = _contained_path(base, record_id, filename)
                if target.is_symlink():
                    raise ValueError("artifact path must not be a symlink")
                os.replace(temporary_path, target)
                return target
            finally:
                # Preserve the original write/replace error. A later scan
                # ignores this reserved temporary-file prefix.
                with suppress(OSError):
                    temporary_path.unlink(missing_ok=True)

    def _get_artifact(self, base: Path, record_id: str) -> Path:
        _validate_component(record_id, "record ID")
        with self._operation_lock:
            directory = _contained_path(base, record_id)
            if directory.is_symlink():
                raise ValueError("record directory must not be a symlink")
            if not directory.is_dir():
                raise FileNotFoundError(f"no artifact directory for {record_id}")

            artifacts = self._published_artifacts(base, directory)
            if not artifacts:
                raise FileNotFoundError(f"no published artifact for {record_id}")
            if len(artifacts) != 1:
                raise FileExistsError(f"multiple published artifacts for {record_id}")
            return artifacts[0]

    @staticmethod
    def _published_artifacts(base: Path, directory: Path) -> list[Path]:
        artifacts: list[Path] = []
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.name.startswith(_TEMP_PREFIX):
                    continue
                candidate = Path(entry.path)
                _assert_contained(base, candidate)
                if entry.is_symlink():
                    raise ValueError("published artifact paths must not be symlinks")
                if entry.is_file(follow_symlinks=False):
                    artifacts.append(candidate)
        return artifacts


def _canonical_base(base: Path) -> Path:
    """Return an absolute canonical base, including existing symlink parents."""
    return Path(os.path.abspath(base)).resolve()


def _validate_component(value: str, label: str) -> None:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{label} must be a non-empty path component")
    windows_path = PureWindowsPath(value)
    if (
        value in {".", ".."}
        or "/" in value
        or "\\" in value
        or Path(value).is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
    ):
        raise ValueError(f"{label} must be a single path component")


def _contained_path(base: Path, *parts: str) -> Path:
    candidate = Path(os.path.abspath(base.joinpath(*parts)))
    resolved = candidate.resolve()
    if not resolved.is_relative_to(base):
        raise ValueError("path traversal detected")
    # Keep the lexical path after checking its canonical target so callers can
    # reject even in-base symlink aliases instead of silently following them.
    return candidate


def _assert_contained(base: Path, candidate: Path) -> None:
    absolute = Path(os.path.abspath(candidate))
    if not absolute.resolve().is_relative_to(base):
        raise ValueError("path traversal detected")
