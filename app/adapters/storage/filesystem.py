"""Unwired filesystem storage skeleton implementing the FileStorage port."""

from pathlib import Path

from app.core.ports import FileStorage


class FilesystemStorage(FileStorage):
    """Skeleton storage adapter; file persistence is not wired yet."""

    async def save_upload(
        self,
        document_id: str,
        content: bytes,
        filename: str,
    ) -> Path:
        raise NotImplementedError("Filesystem storage is not wired yet")

    async def get_upload_path(self, document_id: str) -> Path:
        raise NotImplementedError("Filesystem storage is not wired yet")

    async def save_output(
        self,
        job_id: str,
        content: bytes,
        filename: str,
    ) -> Path:
        raise NotImplementedError("Filesystem storage is not wired yet")

    async def get_output_path(self, job_id: str) -> Path:
        raise NotImplementedError("Filesystem storage is not wired yet")
