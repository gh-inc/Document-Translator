"""Unwired registry skeleton for document format port implementations."""

from pathlib import Path

from app.core.ports import DocumentExtractor, DocumentRenderer
from app.core.ports import FormatRegistry as FormatRegistryPort


class FormatRegistry(FormatRegistryPort):
    """Skeleton registry; format registration and file resolution are not wired yet."""

    def register(
        self,
        format_name: str,
        extractor: DocumentExtractor,
        renderer: DocumentRenderer,
    ) -> None:
        raise NotImplementedError("Format registry is not wired yet")

    async def resolve(
        self,
        file_path: Path,
    ) -> tuple[DocumentExtractor, DocumentRenderer] | None:
        raise NotImplementedError("Format registry is not wired yet")
