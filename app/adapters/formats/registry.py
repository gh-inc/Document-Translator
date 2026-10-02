"""Resolve uploaded files to their registered format adapters."""

import asyncio
from pathlib import Path

from app.core.ports import DocumentExtractor, DocumentRenderer
from app.core.ports import FormatRegistry as FormatRegistryPort

_HEADER_SIZE = 2048
_MAGIC_BY_FORMAT = {
    "pdf": b"%PDF-",
    "docx": b"PK\x03\x04",
}


class FormatRegistry(FormatRegistryPort):
    """Map supported document signatures to extractor and renderer pairs."""

    def __init__(self) -> None:
        self._adapters: dict[str, tuple[DocumentExtractor, DocumentRenderer]] = {}

    def register(
        self,
        format_name: str,
        extractor: DocumentExtractor,
        renderer: DocumentRenderer,
    ) -> None:
        """Register adapters under a normalized format name."""
        normalized_name = format_name.strip().lower().lstrip(".")
        self._adapters[normalized_name] = (extractor, renderer)

    async def resolve(
        self,
        file_path: Path,
    ) -> tuple[DocumentExtractor, DocumentRenderer] | None:
        """Resolve a file using its supported suffix and a bounded signature read."""
        suffix = file_path.suffix.lower().lstrip(".")
        if suffix and suffix not in _MAGIC_BY_FORMAT:
            return None

        try:
            header = await asyncio.to_thread(_read_header, file_path)
        except OSError:
            return None

        if suffix:
            detected_format = suffix
            if not header.startswith(_MAGIC_BY_FORMAT[detected_format]):
                return None
        else:
            detected_format = next(
                (
                    format_name
                    for format_name, signature in _MAGIC_BY_FORMAT.items()
                    if header.startswith(signature)
                ),
                "",
            )

        return self._adapters.get(detected_format)


def _read_header(file_path: Path) -> bytes:
    """Read only the prefix needed to identify a supported document format."""
    with file_path.open("rb") as document_file:
        return document_file.read(_HEADER_SIZE)
