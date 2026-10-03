"""Markdown extraction and rendering with table geometry kept in metadata."""

from __future__ import annotations

import asyncio
import hashlib
import re
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import structlog

from app.core.errors import DocumentError, ErrorCode
from app.core.models import Block, DocumentIR, RenderResult

_logger = structlog.get_logger(__name__)
_FENCE_OPEN = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})(?P<info>.*)$")
_HEADING = re.compile(r"^(?P<indent> {0,3})(?P<marks>#{1,6})(?P<space>[ \t]+)(?P<text>.*)$")
_LIST_ITEM = re.compile(
    r"^(?P<indent> {0,3})(?P<marker>(?:[-*+]|\d+[.)]))(?P<space>[ \t]+)(?P<text>.*)$"
)
_QUOTE = re.compile(r"^(?P<indent> {0,3}>[ \t]?)(?P<text>.*)$")
_SEPARATOR_CELL = re.compile(r":?-{3,}:?")
_LINE_BREAKS = re.compile(r"[\r\n\v\f\x85\u2028\u2029]+")


class MarkdownExtractor:
    """Extract translatable lines and table cells from a Markdown file."""

    def __init__(self) -> None:
        self.skip_block_ids: set[str] = set()

    async def extract(self, file_path: Path, document_id: str) -> DocumentIR:
        try:
            document = await asyncio.to_thread(_extract_sync, file_path, document_id)
        except DocumentError:
            raise
        except Exception:
            raise DocumentError(ErrorCode.CORRUPT_FILE) from None
        self.skip_block_ids = self.get_skip_block_ids(document.blocks)
        return document

    @staticmethod
    def get_skip_block_ids(blocks: Sequence[Block]) -> set[str]:
        """Classify persisted empty table cells without relying on extractor state."""
        return {
            block.id
            for block in blocks
            if block.format_metadata.get("kind") == "table_cell" and not block.source_text.strip()
        }


class MarkdownRenderer:
    """Render translations onto the original Markdown text and table layout."""

    async def render(
        self,
        original_path: Path,
        blocks: list[Block],
        translations: dict[str, str],
        output_path: Path,
    ) -> RenderResult:
        try:
            rendered_path, passthrough_ids = await asyncio.to_thread(
                _render_sync, original_path, blocks, translations, output_path
            )
        except DocumentError:
            raise
        except Exception as error:
            _logger.error(
                "markdown_render_failed", stage="markdown_render", error_type=type(error).__name__
            )
            raise DocumentError(ErrorCode.RENDER_FAILED) from None
        return RenderResult(output_path=rendered_path, passthrough_block_ids=passthrough_ids)


def _extract_sync(file_path: Path, document_id: str) -> DocumentIR:
    with file_path.open("r", encoding="utf-8", newline="") as source_file:
        text = source_file.read()

    blocks: list[Block] = []
    in_fence: tuple[str, int] | None = None
    for line_number, raw_line in enumerate(text.splitlines(keepends=True), start=1):
        line, _ = _split_line_ending(raw_line)

        if in_fence is not None:
            if _closes_fence(line, in_fence):
                in_fence = None
            continue

        fence_match = _FENCE_OPEN.match(line)
        if fence_match is not None:
            fence = fence_match.group("fence")
            # A backtick fence's info string cannot itself contain backticks.
            if fence[0] != "`" or "`" not in fence_match.group("info"):
                in_fence = (fence[0], len(fence))
                continue

        if not line.strip():
            continue

        cells = _parse_table_row(line)
        if cells is not None and cells[1] and cells[2]:
            values = [cell.strip() for cell in cells[0]]
            if values and all(_SEPARATOR_CELL.fullmatch(value) for value in values):
                continue
            _append_table_blocks(blocks, document_id, line_number, cells)
            continue

        prefix, source_text, suffix, metadata = _parse_content_line(line, line_number)
        if not source_text:
            continue
        metadata["prefix"] = prefix
        metadata["suffix"] = suffix
        _append_block(blocks, document_id, source_text, metadata)

    return DocumentIR(
        id=document_id,
        filename=file_path.name,
        format="md",
        size_bytes=file_path.stat().st_size,
        page_count=None,
        blocks=blocks,
    )


def _parse_content_line(line: str, line_number: int) -> tuple[str, str, str, dict[str, Any]]:
    heading = _HEADING.match(line)
    if heading is not None:
        prefix = f"{heading.group('indent')}{heading.group('marks')}{heading.group('space')}"
        source_text, suffix = _trim_text_and_suffix(heading.group("text"))
        return (
            prefix,
            source_text,
            suffix,
            {
                "kind": "heading",
                "line": line_number,
                "level": len(heading.group("marks")),
            },
        )

    list_item = _LIST_ITEM.match(line)
    if list_item is not None:
        prefix = f"{list_item.group('indent')}{list_item.group('marker')}{list_item.group('space')}"
        source_text, suffix = _trim_text_and_suffix(list_item.group("text"))
        return (
            prefix,
            source_text,
            suffix,
            {
                "kind": "list_item",
                "line": line_number,
                "marker": list_item.group("marker"),
            },
        )

    quote = _QUOTE.match(line)
    if quote is not None:
        source_text, suffix = _trim_text_and_suffix(quote.group("text"))
        return quote.group("indent"), source_text, suffix, {"kind": "quote", "line": line_number}

    leading = len(line) - len(line.lstrip())
    prefix = line[:leading]
    source_text, suffix = _trim_text_and_suffix(line[leading:])
    return prefix, source_text, suffix, {"kind": "paragraph", "line": line_number}


def _trim_text_and_suffix(text: str) -> tuple[str, str]:
    source_text = text.strip()
    if not source_text:
        return "", text
    return source_text, text[len(text.rstrip()) :]


def _append_table_blocks(
    blocks: list[Block],
    document_id: str,
    line_number: int,
    parsed: tuple[list[str], bool, bool, str, str],
) -> None:
    raw_cells, leading_pipe, trailing_pipe, outer_prefix, outer_suffix = parsed
    columns = len(raw_cells)
    for column, raw_cell in enumerate(raw_cells):
        source_text = raw_cell.strip()
        if source_text:
            leading_padding = raw_cell[: len(raw_cell) - len(raw_cell.lstrip())]
            trailing_padding = raw_cell[len(raw_cell.rstrip()) :]
        else:
            # Whitespace-only cells have no distinct content boundary. Keep
            # their padding on one side so reconstruction does not duplicate it.
            leading_padding = raw_cell
            trailing_padding = ""
        prefix = ("|" if column > 0 else "") + leading_padding
        suffix = trailing_padding
        if column == 0 and leading_pipe:
            prefix = outer_prefix + "|" + prefix
        if column == columns - 1 and trailing_pipe:
            suffix += "|" + outer_suffix
        _append_block(
            blocks,
            document_id,
            source_text,
            {
                "kind": "table_cell",
                "line": line_number,
                "column": column,
                "columns": columns,
                "prefix": prefix,
                "suffix": suffix,
            },
        )


def _append_block(
    blocks: list[Block], document_id: str, source_text: str, metadata: dict[str, Any]
) -> None:
    seq = len(blocks)
    blocks.append(
        Block(
            id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{document_id}:{seq}")),
            seq=seq,
            source_text=source_text,
            source_hash=hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
            format_metadata=metadata,
        )
    )


def _parse_table_row(line: str) -> tuple[list[str], bool, bool, str, str] | None:
    left_padding_length = len(line) - len(line.lstrip(" \t"))
    right_padding_length = len(line) - len(line.rstrip(" \t"))
    content_end = len(line) - right_padding_length if right_padding_length else len(line)
    outer_prefix = line[:left_padding_length]
    outer_suffix = line[content_end:]
    content = line[left_padding_length:content_end]
    cells: list[str] = []
    start = 0
    escaped = False
    for index, char in enumerate(content):
        if char == "\\":
            escaped = not escaped
            continue
        if char == "|" and not escaped:
            cells.append(content[start:index])
            start = index + 1
        escaped = False
    if start == 0:
        return None
    cells.append(content[start:])

    leading_pipe = content.startswith("|")
    trailing_pipe = content.endswith("|") and not _is_escaped(content, len(content) - 1)
    if leading_pipe:
        cells.pop(0)
    if trailing_pipe:
        cells.pop()
    if not cells:
        return None
    return cells, leading_pipe, trailing_pipe, outer_prefix, outer_suffix


def _is_escaped(text: str, index: int) -> bool:
    slashes = 0
    index -= 1
    while index >= 0 and text[index] == "\\":
        slashes += 1
        index -= 1
    return slashes % 2 == 1


def _split_line_ending(line: str) -> tuple[str, str]:
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith(("\n", "\r")):
        return line[:-1], line[-1:]
    return line, ""


def _closes_fence(line: str, opening: tuple[str, int]) -> bool:
    char, minimum_length = opening
    match = re.match(r"^ {0,3}(?P<fence>`+|~+)[ \t]*$", line)
    return (
        match is not None
        and match.group("fence")[0] == char
        and len(match.group("fence")) >= minimum_length
    )


def _render_sync(
    original_path: Path,
    blocks: list[Block],
    translations: dict[str, str],
    output_path: Path,
) -> tuple[Path, list[str]]:
    with original_path.open("r", encoding="utf-8", newline="") as original_file:
        source = original_file.read()
    lines = source.splitlines(keepends=True)

    line_blocks: dict[int, list[Block]] = {}
    passthrough_ids: list[str] = []
    for block in blocks:
        metadata = block.format_metadata
        kind = metadata.get("kind")
        line_number = _metadata_int(metadata, "line")
        if kind not in {"heading", "list_item", "quote", "paragraph", "table_cell"}:
            raise ValueError("invalid Markdown block kind")
        if line_number < 1 or line_number > len(lines):
            raise ValueError("Markdown line is outside the source document")
        if kind == "table_cell" and not block.source_text.strip():
            passthrough_ids.append(block.id)
        line_blocks.setdefault(line_number, []).append(block)

    rendered: list[str] = []
    for line_number, raw_line in enumerate(lines, start=1):
        original_content, line_ending = _split_line_ending(raw_line)
        related = line_blocks.get(line_number, [])
        if not related:
            rendered.append(raw_line)
            continue

        if all(block.format_metadata.get("kind") == "table_cell" for block in related):
            content = _render_table_line(related, translations)
        elif len(related) == 1:
            block = related[0]
            metadata = block.format_metadata
            content = original_content
            if block.id in translations:
                prefix = _metadata_string(metadata, "prefix")
                suffix = _metadata_string(metadata, "suffix")
                content = prefix + _safe_translation(translations[block.id]) + suffix
        else:
            raise ValueError("multiple Markdown blocks refer to one prose line")
        rendered.append(content + line_ending)

    with output_path.open("w", encoding="utf-8", newline="") as output_file:
        output_file.write("".join(rendered))
    return output_path, passthrough_ids


def _render_table_line(blocks: list[Block], translations: dict[str, str]) -> str:
    ordered = sorted(blocks, key=lambda block: _metadata_int(block.format_metadata, "column"))
    expected_columns = _metadata_int(ordered[0].format_metadata, "columns")
    if expected_columns < 1 or len(ordered) != expected_columns:
        raise ValueError("Markdown table row has incomplete cell metadata")

    cells: list[str] = []
    for expected_column, block in enumerate(ordered):
        metadata = block.format_metadata
        column = _metadata_int(metadata, "column")
        columns = _metadata_int(metadata, "columns")
        if column != expected_column or columns != expected_columns:
            raise ValueError("Markdown table cell metadata is inconsistent")
        prefix = _metadata_string(metadata, "prefix")
        suffix = _metadata_string(metadata, "suffix")
        is_empty_cell = not block.source_text.strip()
        has_translation = block.id in translations and not is_empty_cell
        translated = translations[block.id] if has_translation else block.source_text
        safe_text = _safe_translation(
            translated,
            strip_pipes=has_translation,
            protect_delimiter=has_translation
            and (expected_column < expected_columns - 1 or "|" in suffix),
        )
        cells.append(prefix + safe_text + suffix)
    return "".join(cells)


def _safe_translation(
    text: str,
    *,
    strip_pipes: bool = False,
    protect_delimiter: bool = False,
) -> str:
    if not isinstance(text, str):
        raise ValueError("translation must be text")
    safe_text = _LINE_BREAKS.sub(" ", text)
    if strip_pipes:
        safe_text = safe_text.replace("|", "")
    if protect_delimiter:
        trailing_backslashes = len(safe_text) - len(safe_text.rstrip("\\"))
        if trailing_backslashes % 2:
            safe_text += "\\"
    return safe_text


def _metadata_int(metadata: dict[str, Any], key: str) -> int:
    value = metadata.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("invalid Markdown metadata")
    return value


def _metadata_string(metadata: dict[str, Any], key: str) -> str:
    value = metadata.get(key)
    if not isinstance(value, str):
        raise ValueError("invalid Markdown metadata")
    return value
