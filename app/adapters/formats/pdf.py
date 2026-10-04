"""PDF extraction and rendering adapter backed by PyMuPDF."""

from __future__ import annotations

import asyncio
import hashlib
import math
import unicodedata
import uuid
from pathlib import Path
from typing import Any

import pymupdf
import structlog

from app.core.errors import DocumentError, ErrorCode
from app.core.models import Block, DocumentIR, RenderResult

# A single non-whitespace character is valid text; blank and image-only PDFs
# have no extracted text and are rejected without misclassifying short notes.
_MIN_TEXT_CHARACTERS = 1
_MIN_FONT_SIZE = 6.0
_INITIAL_FONT_SIZE = 11.0
_FONT_SIZE_STEP = 0.5
_logger = structlog.get_logger(__name__)
_RENDER_FONT = pymupdf.Font("cjk")
_KEEP_CONTROL = frozenset({"\n", "\t"})


def _droppable_character(char: str) -> bool:
    code = ord(char)
    return (
        (unicodedata.category(char) in {"Cc", "Cf"} and char not in _KEEP_CONTROL)
        or 0xFE00 <= code <= 0xFE0F
        or 0xE0100 <= code <= 0xE01EF
    )


def _split_unsupported(text: str) -> tuple[str, set[str]]:
    """Remove non-printing artifacts and report visible glyphs the font lacks."""
    cleaned = "".join(char for char in text if not _droppable_character(char))
    unsupported = {
        char for char in cleaned if not char.isspace() and not _RENDER_FONT.has_glyph(ord(char))
    }
    return cleaned, unsupported


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
    def source_warnings(blocks: list[Block]) -> list[str]:
        """Recompute upload warnings from source text, including stored blocks."""
        problematic: set[str] = set()
        for block in blocks:
            _, unsupported = _split_unsupported(block.source_text)
            problematic.update(unsupported)
            problematic.update(char for char in block.source_text if _droppable_character(char))
        if not problematic:
            return []
        codes = ", ".join(f"U+{ord(char):04X}" for char in sorted(problematic))
        return [f"PDF source contains characters requiring rendering fallback or cleanup: {codes}"]

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
                warnings=PdfExtractor.source_warnings(blocks),
            )


class PdfRenderer:
    """Replace translated PDF text while preserving the original page artwork."""

    async def render(
        self,
        original_path: Path,
        blocks: list[Block],
        translations: dict[str, str],
        output_path: Path,
    ) -> RenderResult:
        try:
            fallback_count, fallback_pages_count, degraded_block_ids = await asyncio.to_thread(
                self._render_sync, original_path, blocks, translations, output_path
            )
        except DocumentError:
            raise
        except Exception as error:
            _logger.error("pdf_render_failed", stage="render", error_type=type(error).__name__)
            raise DocumentError(ErrorCode.RENDER_FAILED) from None

        _logger.info(
            "pdf_render_completed",
            fallback_count=fallback_count,
            fallback_pages_count=fallback_pages_count,
            degraded_block_count=len(degraded_block_ids),
        )
        return RenderResult(
            output_path=output_path,
            degraded_block_ids=degraded_block_ids,
            fallback_blocks=fallback_count,
            fallback_pages=fallback_pages_count,
        )

    @staticmethod
    def _render_sync(
        original_path: Path,
        blocks: list[Block],
        translations: dict[str, str],
        output_path: Path,
    ) -> tuple[int, int, list[str]]:
        with pymupdf.open(original_path) as document:
            # PyMuPDF's bundled Droid Sans Fallback supports Latin, Cyrillic,
            # and CJK without relying on host-installed font packages. Embed
            # that exact buffer under a private name so measured and inserted
            # glyph widths use the same font data.
            font_name = "dtransunicode"
            font = _RENDER_FONT
            font_buffer = font.buffer
            render_items: list[tuple[int, pymupdf.Rect, str]] = []
            degraded_block_ids: list[str] = []
            protected_by_page: dict[int, list[pymupdf.Rect]] = {}

            for block in blocks:
                translated_text = translations.get(block.id)
                if translated_text is None:
                    continue
                page_number, rectangle = _metadata_rectangle(block, document)
                cleaned, unsupported = _split_unsupported(translated_text)
                if unsupported:
                    _logger.warning(
                        "pdf_block_degraded",
                        block_id=block.id,
                        unsupported_codes=[f"U+{ord(char):04X}" for char in sorted(unsupported)],
                    )
                    degraded_block_ids.append(block.id)
                    protected_by_page.setdefault(page_number, []).append(rectangle)
                    continue
                render_items.append((page_number, rectangle, cleaned))

            # Redact only PDF text. Disabling image and graphics redaction keeps
            # raster images and vector artwork on the original canvas.
            items_by_page: dict[int, list[tuple[pymupdf.Rect, str]]] = {}
            for page_number, rectangle, translated_text in render_items:
                items_by_page.setdefault(page_number, []).append((rectangle, translated_text))
            for page_number, items in items_by_page.items():
                page = document[page_number]
                spans = _page_spans(page)
                protected = protected_by_page.get(page_number, [])
                for rectangle, _ in items:
                    # Extracted block bboxes overlap, so redacting a block's own
                    # rectangle would also delete the text of any block whose
                    # glyphs fall inside that bleed. Clip the redaction against
                    # every span the block does not own, so each span is erased
                    # exactly when its own block is redacted.
                    for redaction in _redaction_rectangles(rectangle, spans, protected):
                        page.add_redact_annot(redaction, fill=False, cross_out=False)
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
            try:
                document.save(output_path, garbage=4, deflate=True)
            except Exception as error:
                _logger.error("pdf_render_failed", stage="save", error_type=type(error).__name__)
                raise DocumentError(ErrorCode.RENDER_FAILED) from None
            return fallback_count, fallback_pages_count, degraded_block_ids


def _page_spans(page: pymupdf.Page) -> list[pymupdf.Rect]:
    """Return every text span on a page as a rectangle."""
    spans: list[pymupdf.Rect] = []
    extracted = page.get_text("dict")
    for block in extracted["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                rectangle = pymupdf.Rect(span["bbox"])
                if not rectangle.is_empty:
                    spans.append(rectangle)
    return spans


def _is_owned_by(span: pymupdf.Rect, rectangle: pymupdf.Rect) -> bool:
    """Report whether a span belongs to the block rectangle being redacted.

    Ownership is decided per span against one rectangle at a time: a span is the
    block's own text when the rectangle contains at least 90% of it. Spans of a
    neighbouring block that merely fall into the bleed of an overlapping
    rectangle score below the threshold and are therefore treated as foreign.
    """
    area = span.get_area()
    if area <= 0:
        return False
    overlap = span & rectangle
    return not overlap.is_empty and overlap.get_area() >= 0.9 * area


def _subtract_rectangles(
    pieces: list[pymupdf.Rect], regions: list[pymupdf.Rect]
) -> list[pymupdf.Rect]:
    """Remove every region from the current redaction pieces."""
    for region in regions:
        remaining: list[pymupdf.Rect] = []
        for piece in pieces:
            intersection = piece & region
            if intersection.is_empty:
                remaining.append(piece)
                continue
            candidates = [
                pymupdf.Rect(piece.x0, piece.y0, piece.x1, intersection.y0),
                pymupdf.Rect(piece.x0, intersection.y1, piece.x1, piece.y1),
                pymupdf.Rect(piece.x0, intersection.y0, intersection.x0, intersection.y1),
                pymupdf.Rect(intersection.x1, intersection.y0, piece.x1, intersection.y1),
            ]
            remaining.extend(candidate for candidate in candidates if not candidate.is_empty)
        pieces = remaining
        if not pieces:
            break
    return pieces


def _redaction_rectangles(
    rectangle: pymupdf.Rect,
    spans: list[pymupdf.Rect],
    protected: list[pymupdf.Rect],
) -> list[pymupdf.Rect]:
    """Clip a block's redaction against retained source regions.

    A block's own characters lie inside its own spans, so subtracting foreign
    spans removes exactly the block's own source text and never a neighbour's.
    `protected` carries whole rectangles for blocks that stay as source because
    their translation could not be rendered; those need the stronger guarantee
    DT-77 established, since a synthetic or oversized rectangle can contain
    their spans entirely.
    """
    pieces = _subtract_rectangles([rectangle], protected)
    for span in spans:
        if _is_owned_by(span, rectangle):
            continue
        pieces = _subtract_rectangles(pieces, [span])
        if not pieces:
            break
    return pieces


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
    ):
        fields = {"page": page_number} if type(page_number) is int else {}
        _logger.error("pdf_render_failed", stage="metadata", reason="page_out_of_range", **fields)
        raise DocumentError(ErrorCode.RENDER_FAILED)

    if (
        not isinstance(bbox, list | tuple)
        or len(bbox) != 4
        or any(type(value) not in {int, float} for value in bbox)
    ):
        _logger.error("pdf_render_failed", stage="metadata", reason="bbox_shape", page=page_number)
        raise DocumentError(ErrorCode.RENDER_FAILED)
    try:
        coordinates = tuple(float(value) for value in bbox)
    except OverflowError:
        coordinates = ()
    if not coordinates or not all(math.isfinite(value) for value in coordinates):
        _logger.error("pdf_render_failed", stage="metadata", reason="not_finite", page=page_number)
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
        _logger.error(
            "pdf_render_failed",
            stage="metadata",
            reason="out_of_bounds",
            page=page_number,
            bbox=list(coordinates),
            page_rect=list(page_rect),
        )
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
        _logger.error("pdf_render_failed", stage="fallback", reason="page_too_small")
        raise DocumentError(ErrorCode.RENDER_FAILED)

    lines = _wrap_text(text, font, font_size, available_width)
    lines_per_page = int((page_height - 2 * margin) // line_height)
    if lines_per_page < 1:
        _logger.error("pdf_render_failed", stage="fallback", reason="lines_per_page")
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
                        _logger.error("pdf_render_failed", stage="wrap", reason="char_too_wide")
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
