"""Markdown adapter structure, passthrough, and safety tests."""

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest

from app.adapters.formats import markdown as markdown_adapter
from app.core.errors import DocumentError, ErrorCode
from app.core.models import Block


def _read_table_rows(text: str) -> list[list[str]]:
    return [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in text.splitlines()
        if "|" in line and not line.lstrip().startswith("```")
    ]


@pytest.mark.asyncio
async def test_sample_round_trip_preserves_structure_and_translates_content(
    tmp_path: Path,
) -> None:
    source = Path("samples/sample_en.md")
    output = tmp_path / "translated.md"
    extractor = markdown_adapter.MarkdownExtractor()
    document = await extractor.extract(source, "sample-document")
    translations = {
        block.id: f"translated {block.source_text}"
        for block in document.blocks
        if block.source_text.strip()
    }

    result = await markdown_adapter.MarkdownRenderer().render(
        source, document.blocks, translations, output
    )

    rendered = await asyncio.to_thread(output.read_text, encoding="utf-8")
    original = await asyncio.to_thread(source.read_text, encoding="utf-8")
    assert result.output_path == output
    assert result.passthrough_block_ids == [
        block.id for block in document.blocks if block.id in extractor.skip_block_ids
    ]
    assert "## translated Overview" in rendered
    assert "- translated First bullet item" in rendered
    assert "1. translated Numbered item one" in rendered
    assert "> translated A quoted sentence for translation." in rendered
    assert '```text\nDo not translate this code: print("hello")\n```' in rendered
    assert "| :--- | :---: | ---: |" in rendered
    assert len(_read_table_rows(rendered)) == len(_read_table_rows(original))
    assert [len(row) for row in _read_table_rows(rendered)] == [
        len(row) for row in _read_table_rows(original)
    ]


@pytest.mark.asyncio
async def test_extractor_keeps_empty_cells_but_marks_persisted_ids_for_bypass(
    tmp_path: Path,
) -> None:
    source = tmp_path / "table.md"
    source.write_text("| A |  | C |\n| --- | --- | --- |\n|  | B |  |\n", encoding="utf-8")
    extractor = markdown_adapter.MarkdownExtractor()

    document = await extractor.extract(source, "table-document")
    empty_cells = [
        block
        for block in document.blocks
        if block.format_metadata.get("kind") == "table_cell" and not block.source_text
    ]

    assert len(document.blocks) == 6
    assert len(empty_cells) == 3
    assert extractor.skip_block_ids == {block.id for block in empty_cells}
    assert markdown_adapter.MarkdownExtractor.get_skip_block_ids(document.blocks) == {
        block.id for block in empty_cells
    }
    repeated = await extractor.extract(source, "table-document")
    assert [block.id for block in repeated.blocks] == [block.id for block in document.blocks]
    assert [block.format_metadata["line"] for block in document.blocks] == [1, 1, 1, 3, 3, 3]


@pytest.mark.asyncio
async def test_pipe_rows_keep_escaped_pipes_and_cell_geometry(tmp_path: Path) -> None:
    source = tmp_path / "escaped.md"
    output = tmp_path / "escaped-out.md"
    source.write_text("| left \\| inside | right |\n", encoding="utf-8")
    document = await markdown_adapter.MarkdownExtractor().extract(source, "escaped")
    assert [block.source_text for block in document.blocks] == ["left \\| inside", "right"]

    await markdown_adapter.MarkdownRenderer().render(
        source,
        document.blocks,
        {document.blocks[1].id: "dropping | a pipe"},
        output,
    )

    assert await asyncio.to_thread(output.read_text, encoding="utf-8") == (
        "| left \\| inside | dropping  a pipe |\n"
    )


@pytest.mark.asyncio
async def test_prose_pipe_is_not_a_table_and_outer_pipe_rows_keep_padding(
    tmp_path: Path,
) -> None:
    source = tmp_path / "prose-pipe.md"
    output = tmp_path / "prose-pipe-out.md"
    source.write_text("Compare A | B before deploying.\n  | A | B |  \n", encoding="utf-8")
    document = await markdown_adapter.MarkdownExtractor().extract(source, "prose-pipe")

    assert [block.format_metadata["kind"] for block in document.blocks] == [
        "paragraph",
        "table_cell",
        "table_cell",
    ]
    await markdown_adapter.MarkdownRenderer().render(
        source,
        document.blocks,
        {
            document.blocks[0].id: "Compare C | D before release.",
            document.blocks[1].id: "Field translated",
            document.blocks[2].id: "Value translated",
        },
        output,
    )

    assert await asyncio.to_thread(output.read_text, encoding="utf-8") == (
        "Compare C | D before release.\n  | Field translated | Value translated |  \n"
    )


@pytest.mark.asyncio
async def test_compact_table_translation_cannot_escape_following_delimiter(tmp_path: Path) -> None:
    source = tmp_path / "compact.md"
    output = tmp_path / "compact-out.md"
    source.write_text("|a|b|\n", encoding="utf-8")
    document = await markdown_adapter.MarkdownExtractor().extract(source, "compact")

    await markdown_adapter.MarkdownRenderer().render(
        source, document.blocks, {document.blocks[0].id: "evil\\"}, output
    )

    rendered = await asyncio.to_thread(output.read_text, encoding="utf-8")
    assert rendered == "|evil\\\\|b|\n"
    assert len(_read_table_rows(rendered)[0]) == 2


@pytest.mark.asyncio
async def test_renderer_preserves_crlf_and_missing_translations_and_sanitizes_newlines(
    tmp_path: Path,
) -> None:
    source = tmp_path / "crlf.md"
    output = tmp_path / "crlf-out.md"
    source.write_bytes(b"# Heading\r\n\r\n| A | B |\r\n| --- | --- |\r\n| source | other |\r\n")
    document = await markdown_adapter.MarkdownExtractor().extract(source, "crlf")
    table_blocks = [
        block for block in document.blocks if block.format_metadata["kind"] == "table_cell"
    ]
    translated = {
        table_blocks[-2].id: "first\r\n| forged row\nend",
    }

    await markdown_adapter.MarkdownRenderer().render(source, document.blocks, translated, output)

    result = output.read_bytes()
    assert result.endswith(b"\r\n")
    assert result.count(b"\r\n") == source.read_bytes().count(b"\r\n")
    assert b"\n" not in result.replace(b"\r\n", b"")
    assert result.decode().splitlines()[-1] == "| first  forged row end | other |"
    assert result.decode().splitlines()[-2] == "| --- | --- |"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "opening,short_closer,valid_closer",
    [("```python", "``", "````"), ("~~~~js", "~~~", "~~~~~")],
)
async def test_fenced_code_requires_matching_nonshorter_closer(
    tmp_path: Path, opening: str, short_closer: str, valid_closer: str
) -> None:
    source = tmp_path / "fence.md"
    source.write_text(
        f"{opening}\nKeep one\n{short_closer}\nKeep two\n{valid_closer}\nAfter fence\n",
        encoding="utf-8",
    )
    document = await markdown_adapter.MarkdownExtractor().extract(source, "fences")

    assert [block.source_text for block in document.blocks] == ["After fence"]


@pytest.mark.asyncio
async def test_renderer_rejects_bad_metadata_with_catalogued_error(tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    source.write_text("hello\n", encoding="utf-8")
    block = Block(
        id="bad-block",
        seq=0,
        source_text="hello",
        source_hash="hash",
        format_metadata={"kind": "paragraph", "line": True, "prefix": "", "suffix": ""},
    )

    with pytest.raises(DocumentError) as error:
        await markdown_adapter.MarkdownRenderer().render(
            source, [block], {block.id: "bonjour"}, tmp_path / "out.md"
        )

    assert error.value.error_code is ErrorCode.RENDER_FAILED
    assert "invalid Markdown metadata" not in str(error.value)


@pytest.mark.asyncio
async def test_markdown_file_io_runs_in_worker_threads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.md"
    output = tmp_path / "output.md"
    source.write_text("hello\n", encoding="utf-8")
    main_thread = threading.get_ident()
    read_threads: list[int] = []
    original_open = Path.open

    def tracking_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path in {source, output}:
            read_threads.append(threading.get_ident())
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", tracking_open)
    document = await markdown_adapter.MarkdownExtractor().extract(source, "threads")
    await markdown_adapter.MarkdownRenderer().render(source, document.blocks, {}, output)

    assert len(read_threads) == 3
    assert all(thread_id != main_thread for thread_id in read_threads)


@pytest.mark.asyncio
async def test_platon_fixture_has_passthrough_separator_and_eight_empty_cells(
    tmp_path: Path,
) -> None:
    source = Path("samples/platon-complex.md")
    output = tmp_path / "platon-out.md"
    document = await markdown_adapter.MarkdownExtractor().extract(source, "platon-markdown")
    original = await asyncio.to_thread(source.read_text, encoding="utf-8")
    table_cells = [
        block for block in document.blocks if block.format_metadata["kind"] == "table_cell"
    ]
    empty_cells = [block for block in table_cells if not block.source_text]
    separator_lines = {
        line_number
        for line_number, line in enumerate(original.splitlines(), start=1)
        if all(
            cell.strip().replace(":", "").replace("-", "") == ""
            for cell in line.strip().strip("|").split("|")
        )
        and "-" in line
    }

    assert len(empty_cells) == 8
    assert not separator_lines.intersection(block.format_metadata["line"] for block in table_cells)
    result = await markdown_adapter.MarkdownRenderer().render(source, document.blocks, {}, output)
    rendered_lines = await asyncio.to_thread(output.read_text, encoding="utf-8")
    original_lines = original.splitlines(keepends=True)
    translated_lines = rendered_lines.splitlines(keepends=True)

    assert result.passthrough_block_ids == [block.id for block in empty_cells]
    assert all(
        translated_lines[line_number - 1] == original_lines[line_number - 1]
        for line_number in separator_lines
    )


@pytest.mark.asyncio
async def test_eleven_by_three_table_bypasses_twenty_empty_cells(tmp_path: Path) -> None:
    source = tmp_path / "merged-cells.md"
    output = tmp_path / "merged-cells-out.md"
    rows = [
        ["Heading A", "Heading B", "Heading C"],
        ["---", "---", "---"],
        ["first", "second", "third"],
        ["row 2", "", ""],
        *[[f"row {row_number}", "", ""] for row_number in range(3, 12)],
    ]
    source.write_text(
        "\n".join("|" + "|".join(row) + "|" for row in rows) + "\n",
        encoding="utf-8",
    )
    extractor = markdown_adapter.MarkdownExtractor()
    document = await extractor.extract(source, "merged-cells")
    table_cells = [
        block for block in document.blocks if block.format_metadata["kind"] == "table_cell"
    ]
    empty_cells = [block for block in table_cells if not block.source_text]
    translations = {
        block.id: f"translated {block.source_text}" for block in table_cells if block.source_text
    }

    result = await markdown_adapter.MarkdownRenderer().render(
        source, document.blocks, translations, output
    )

    rendered = await asyncio.to_thread(output.read_text, encoding="utf-8")
    output_rows = [line for line in rendered.splitlines() if "|" in line]
    assert len(table_cells) == 36
    assert len(empty_cells) == 20
    assert result.passthrough_block_ids == [block.id for block in empty_cells]
    assert len(output_rows) == 13
    for line in output_rows:
        parsed = markdown_adapter._parse_table_row(line)
        assert parsed is not None
        assert len(parsed[0]) == 3


@pytest.mark.asyncio
async def test_all_empty_cell_translations_keep_row_shape_and_final_newline_policy(
    tmp_path: Path,
) -> None:
    source = tmp_path / "empty.md"
    output = tmp_path / "empty-out.md"
    source.write_bytes(b"|a||b|\n|---|---|---|\n|x||y|")
    document = await markdown_adapter.MarkdownExtractor().extract(source, "empty")
    translations = {
        block.id: "" for block in document.blocks if block.format_metadata["kind"] == "table_cell"
    }
    empty_source_cell = next(block for block in document.blocks if not block.source_text)
    translations[empty_source_cell.id] = "must stay empty"

    await markdown_adapter.MarkdownRenderer().render(source, document.blocks, translations, output)

    rendered = await asyncio.to_thread(output.read_bytes)
    assert rendered == b"||||\n|---|---|---|\n||||"
    parsed_header = markdown_adapter._parse_table_row(rendered.decode().splitlines()[0])
    parsed_data = markdown_adapter._parse_table_row(rendered.decode().splitlines()[-1])
    assert parsed_header is not None and len(parsed_header[0]) == 3
    assert parsed_data is not None and len(parsed_data[0]) == 3
