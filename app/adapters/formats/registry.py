"""Resolve uploaded files to their registered format adapters."""

import asyncio
import codecs
import os
from pathlib import Path

from app.core.models import Block
from app.core.ports import DocumentExtractor, DocumentRenderer
from app.core.ports import FormatRegistry as FormatRegistryPort

_HEADER_SIZE = 2048
_MAGIC_BY_FORMAT = {
    "pdf": b"%PDF-",
    "docx": b"PK\x03\x04",
}
_TEXT_FORMATS = frozenset({"md"})


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
        if suffix and suffix not in _MAGIC_BY_FORMAT and suffix not in _TEXT_FORMATS:
            return None

        try:
            header, at_eof = await asyncio.to_thread(_read_header, file_path)
        except OSError:
            return None

        if suffix:
            detected_format = suffix
            if detected_format in _TEXT_FORMATS:
                if not _is_valid_text_header(header, at_eof=at_eof):
                    return None
            elif not header.startswith(_MAGIC_BY_FORMAT[detected_format]):
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

    def get_skip_block_ids(self, format_name: str, blocks: list[Block]) -> set[str]:
        """Return format-owned structural blocks that must bypass translation."""
        normalized_name = format_name.strip().lower().lstrip(".")
        if normalized_name != "md":
            return set()
        adapter = self._adapters.get(normalized_name)
        if adapter is None:
            return set()
        get_skip_block_ids = getattr(adapter[0], "get_skip_block_ids", None)
        if get_skip_block_ids is None:
            return set()
        return set(get_skip_block_ids(blocks))


def _read_header(file_path: Path) -> tuple[bytes, bool]:
    """Read only the prefix needed to identify a supported document format."""
    with file_path.open("rb") as document_file:
        header = document_file.read(_HEADER_SIZE)
        at_eof = os.fstat(document_file.fileno()).st_size <= len(header)
        return header, at_eof


def _is_valid_text_header(header: bytes, *, at_eof: bool) -> bool:
    """Check UTF-8 and reject NULs, allowing a code point cut at byte 2048."""
    if b"\x00" in header:
        return False
    try:
        decoder = codecs.getincrementaldecoder("utf-8")()
        decoder.decode(header, final=at_eof)
    except UnicodeDecodeError:
        return False
    return True
