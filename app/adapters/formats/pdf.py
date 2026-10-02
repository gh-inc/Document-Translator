"""PDF extraction and rendering adapter backed by PyMuPDF."""

from __future__ import annotations

import asyncio
import hashlib
import math
import uuid
from pathlib import Path
from typing import Any

import pymupdf
import structlog

from app.core.errors import DocumentError, ErrorCode
from app.core.models import Block, DocumentIR

# A single non-whitespace character is valid text; blank and image-only PDFs
# have no extracted text and are rejected without misclassifying short notes.
_MIN_TEXT_CHARACTERS = 1
_MIN_FONT_SIZE = 6.0
_INITIAL_FONT_SIZE = 11.0
_FONT_SIZE_STEP = 0.5
_logger = structlog.get_logger(__name__)


class PdfExtractor:
    """Extract text blocks in page reading order, retaining opaque PDF metadata."""

    async def extract(self, file_path: Path, document_id: str) -> DocumentIR:
        try:
            return await asyncio.to_thread(self._extract_sync, file_path, document_id)
        except DocumentError:
            raise
        except Exception:
            raise DocumentError(ErrorCode.CORRUPT_FILE) from None

    @staticmethod
    def _extract_sync(file_path: Path, document_id: str) -> DocumentIR:
        with pymupdf.open(file_path) as document:
            if document.is_encrypted or document.page_count < 1:
                raise DocumentError(ErrorCode.CORRUPT_FILE)

            blocks: list[Block] = []
            text_character_count = 0
            for page_number, page in enumerate(document):
                for raw_block in page.get_text("blocks", sort=True):
                    x0, y0, x1, y1, text = raw_block[:5]
                    block_type = raw_block[6] if len(raw_block) > 6 else 0
                    if block_type != 0 or not isinstance(text, str):
                        continue
                    source_text = text.strip()
                    if not source_text:
                        continue
                    rectangle = pymupdf.Rect(x0, y0, x1, y1) & page.rect
                    if rectangle.is_empty or rectangle.is_infinite:
                        continue

                    seq = len(blocks)
                    text_character_count += sum(not char.isspace() for char in source_text)
                    block_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{document_id}:{seq}"))
                    blocks.append(
                        Block(
                            id=block_id,
                            seq=seq,
                            source_text=source_text,
                            source_hash=hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
                            format_metadata={
                                "page": page_number,
                                "bbox": [float(value) for value in rectangle],
                            },
                        )
                    )

            if text_character_count < _MIN_TEXT_CHARACTERS:
                raise DocumentError(ErrorCode.SCANNED_PDF)

            return DocumentIR(
                id=document_id,
                filename=file_path.name,
                format="pdf",
                size_bytes=file_path.stat().st_size,
                page_count=document.page_count,
                blocks=blocks,
            )


class PdfRenderer:
    """Replace translated PDF text while preserving the original page artwork."""

    async def render(
        self,
        original_path: Path,
        blocks: list[Block],
        translations: dict[str, str],
        output_path: Path,
    ) -> Path:
        try:
            fallback_count, fallback_pages_count = await asyncio.to_thread(
                self._render_sync, original_path, blocks, translations, output_path
            )
        except DocumentError:
            raise
        except Exception:
            raise DocumentError(ErrorCode.RENDER_FAILED) from None

        _logger.info(
            "pdf_render_completed",
            fallback_count=fallback_count,
            fallback_pages_count=fallback_pages_count,
        )
        return output_path

    @staticmethod
    def _render_sync(
        original_path: Path,
        blocks: list[Block],
        translations: dict[str, str],
        output_path: Path,
    ) -> tuple[int, int]:
        with pymupdf.open(original_path) as document:
            # PyMuPDF's bundled Droid Sans Fallback supports Latin, Cyrillic,
            # and CJK without relying on host-installed font packages. Embed
            # that exact buffer under a private name so measured and inserted
            # glyph widths use the same font data.
            font_name = "dtransunicode"
            font = pymupdf.Font("cjk")
            font_buffer = font.buffer
            render_items: list[tuple[int, pymupdf.Rect, str]] = []

            for block in blocks:
                translated_text = translations.get(block.id)
                if translated_text is None:
                    continue
                if any(
                    not font.has_glyph(ord(char)) for char in translated_text if not char.isspace()
                ):
                    raise DocumentError(ErrorCode.RENDER_FAILED)
                page_number, rectangle = _metadata_rectangle(block, document)
                render_items.append((page_number, rectangle, translated_text))

            # Redact only PDF text. Disabling image and graphics redaction keeps
            # raster images and vector artwork on the original canvas.
            items_by_page: dict[int, list[tuple[pymupdf.Rect, str]]] = {}
            for page_number, rectangle, translated_text in render_items:
                items_by_page.setdefault(page_number, []).append((rectangle, translated_text))
            for page_number, items in items_by_page.items():
                page = document[page_number]
                for rectangle, _ in items:
                    page.add_redact_annot(rectangle, fill=False, cross_out=False)
                page.apply_redactions(images=0, graphics=0, text=0)

            fallback_count = 0
            fallback_pages_count = 0
            for page_number, rectangle, translated_text in render_items:
                if not translated_text.strip():
                    continue
                page = document[page_number]
                _register_font(page, font_name, font_buffer)
                font_size = min(_INITIAL_FONT_SIZE, max(_MIN_FONT_SIZE, rectangle.height * 0.8))
                placed = False
                while True:
                    remaining = page.insert_textbox(
                        rectangle,
                        translated_text,
                        fontname=font_name,
                        fontsize=font_size,
                        color=(0, 0, 0),
                        overlay=True,
                    )
                    if remaining >= 0:
                        placed = True
                        break
                    if font_size <= _MIN_FONT_SIZE:
                        break
                    font_size = max(
                        _MIN_FONT_SIZE,
                        round(font_size - _FONT_SIZE_STEP, 2),
                    )

                if not placed:
                    fallback_count += 1
                    fallback_pages_count += _append_paginated_text(
                        document,
                        translated_text,
                        font,
                        font_name,
                        font_buffer,
                        page.rect.width,
                        page.rect.height,
                    )

            output_path.parent.mkdir(parents=True, exist_ok=True)
            document.save(output_path, garbage=4, deflate=True)
            return fallback_count, fallback_pages_count


def _metadata_rectangle(block: Block, document: pymupdf.Document) -> tuple[int, pymupdf.Rect]:
    """Validate this adapter's opaque page/bbox metadata before modifying a PDF."""
    metadata: dict[str, Any] = block.format_metadata
    page_number = metadata.get("page")
    bbox = metadata.get("bbox")
    if (
        isinstance(page_number, bool)
        or not isinstance(page_number, int)
        or page_number < 0
        or page_number >= document.page_count
        or not isinstance(bbox, list | tuple)
        or len(bbox) != 4
    ):
        raise DocumentError(ErrorCode.RENDER_FAILED)

    try:
        coordinates = tuple(float(value) for value in bbox)
    except (TypeError, ValueError, OverflowError):
        raise DocumentError(ErrorCode.RENDER_FAILED) from None
    if not all(math.isfinite(value) for value in coordinates):
        raise DocumentError(ErrorCode.RENDER_FAILED)

    rectangle = pymupdf.Rect(coordinates)
    page_rect = document[page_number].rect
    if (
        rectangle.is_empty
        or rectangle.is_infinite
        or rectangle.x0 < page_rect.x0
        or rectangle.y0 < page_rect.y0
        or rectangle.x1 > page_rect.x1
        or rectangle.y1 > page_rect.y1
    ):
        raise DocumentError(ErrorCode.RENDER_FAILED)
    return page_number, rectangle


def _append_paginated_text(
    document: pymupdf.Document,
    text: str,
    font: pymupdf.Font,
    font_name: str,
    font_buffer: bytes,
    page_width: float,
    page_height: float,
) -> int:
    """Append every translated character on one or more readable fallback pages."""
    font_size = 10.0
    margin = 36.0
    line_height = font_size * 1.25
    available_width = page_width - (2 * margin)
    if available_width <= 0 or page_height <= 2 * margin + line_height:
        raise DocumentError(ErrorCode.RENDER_FAILED)

    lines = _wrap_text(text, font, font_size, available_width)
    lines_per_page = int((page_height - 2 * margin) // line_height)
    if lines_per_page < 1:
        raise DocumentError(ErrorCode.RENDER_FAILED)

    page_count = 0
    for offset in range(0, len(lines), lines_per_page):
        page = document.new_page(width=page_width, height=page_height)
        _register_font(page, font_name, font_buffer)
        page_count += 1
        page_lines = lines[offset : offset + lines_per_page]
        baseline = margin + font.ascender * font_size
        for line in page_lines:
            if line:
                page.insert_text(
                    (margin, baseline),
                    line,
                    fontname=font_name,
                    fontsize=font_size,
                    color=(0, 0, 0),
                    overlay=True,
                )
            baseline += line_height
    return page_count


def _register_font(page: pymupdf.Page, font_name: str, font_buffer: bytes) -> None:
    page.insert_font(fontname=font_name, fontbuffer=font_buffer)


def _wrap_text(text: str, font: pymupdf.Font, font_size: float, max_width: float) -> list[str]:
    """Wrap words to measured line widths, splitting overlong words by character."""
    lines: list[str] = []
    for paragraph in text.splitlines() or [text]:
        words = paragraph.split()
        if not words:
            lines.append("")
            continue

        current_line = ""
        for word in words:
            if font.text_length(word, fontsize=font_size) > max_width:
                if current_line:
                    lines.append(current_line)
                    current_line = ""
                pieces: list[str] = []
                piece = ""
                for character in word:
                    candidate = piece + character
                    if piece and font.text_length(candidate, fontsize=font_size) > max_width:
                        pieces.append(piece)
                        piece = character
                    else:
                        piece = candidate
                    if font.text_length(piece, fontsize=font_size) > max_width:
                        raise DocumentError(ErrorCode.RENDER_FAILED)
                if piece:
                    pieces.append(piece)
                lines.extend(pieces[:-1])
                current_line = pieces[-1] if pieces else ""
                continue

            candidate_line = f"{current_line} {word}" if current_line else word
            if current_line and font.text_length(candidate_line, fontsize=font_size) > max_width:
                lines.append(current_line)
                current_line = word
            else:
                current_line = candidate_line
        if current_line:
            lines.append(current_line)

    return lines
