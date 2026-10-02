"""DOCX paragraph extraction and translation rendering."""

import asyncio
import hashlib
import uuid
from pathlib import Path
from typing import Any

from docx import Document

from app.core.errors import DocumentError, ErrorCode
from app.core.models import Block, DocumentIR


class DocxExtractor:
    """Extract top-level document paragraphs as opaque-metadata blocks."""

    async def extract(self, file_path: Path, document_id: str) -> DocumentIR:
        try:
            return await asyncio.to_thread(_extract_sync, file_path, document_id)
        except Exception:
            raise DocumentError(ErrorCode.CORRUPT_FILE) from None


class DocxRenderer:
    """Replace translated paragraph content on the original DOCX canvas."""

    async def render(
        self,
        original_path: Path,
        blocks: list[Block],
        translations: dict[str, str],
        output_path: Path,
    ) -> Path:
        try:
            return await asyncio.to_thread(
                _render_sync,
                original_path,
                blocks,
                translations,
                output_path,
            )
        except Exception:
            raise DocumentError(ErrorCode.RENDER_FAILED) from None


def _extract_sync(file_path: Path, document_id: str) -> DocumentIR:
    document = Document(str(file_path))
    blocks: list[Block] = []

    for paragraph_index, paragraph in enumerate(document.paragraphs):
        source_text = paragraph.text.strip()
        if not source_text:
            continue

        seq = len(blocks)
        style = paragraph.style
        blocks.append(
            Block(
                id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{document_id}:{seq}")),
                seq=seq,
                source_text=source_text,
                source_hash=hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
                format_metadata={
                    "paragraph_index": paragraph_index,
                    "style": style.name if style is not None else "Normal",
                },
            )
        )

    return DocumentIR(
        id=document_id,
        filename=file_path.name,
        format="docx",
        size_bytes=file_path.stat().st_size,
        page_count=None,
        blocks=blocks,
    )


def _render_sync(
    original_path: Path,
    blocks: list[Block],
    translations: dict[str, str],
    output_path: Path,
) -> Path:
    document = Document(str(original_path))
    replacements: dict[int, str] = {}

    for block in blocks:
        if block.id not in translations:
            continue

        paragraph_index = _paragraph_index(block.format_metadata)
        if paragraph_index >= len(document.paragraphs):
            raise ValueError("paragraph index is outside the document")
        if paragraph_index in replacements:
            raise ValueError("multiple blocks refer to one paragraph")

        translated_text = translations[block.id]
        if not isinstance(translated_text, str):
            raise ValueError("translation must be text")
        replacements[paragraph_index] = translated_text

    for paragraph_index, translated_text in replacements.items():
        paragraph = document.paragraphs[paragraph_index]
        paragraph.clear()
        paragraph.add_run(translated_text)

    document.save(str(output_path))
    return output_path


def _paragraph_index(metadata: dict[str, Any]) -> int:
    value = metadata.get("paragraph_index")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("invalid paragraph index metadata")
    style = metadata.get("style")
    if not isinstance(style, str):
        raise ValueError("invalid paragraph style metadata")
    return value
