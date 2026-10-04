"""Focused probes for semantic cache mapping and persisted layout metadata."""

from __future__ import annotations

import math
from pathlib import Path

import pymupdf
import pytest

from app.adapters.formats.docx import DocxExtractor
from app.adapters.formats.pdf import PdfExtractor, PdfRenderer
from app.adapters.persistence.database import SqliteConnectionFactory, transaction
from app.adapters.persistence.repositories import (
    SqliteDocumentRepository,
    SqliteTranslationCacheRepository,
)
from app.core.models import TranslationPlan
from app.core.services.cache_keys import translation_key

SAMPLES = Path(__file__).resolve().parents[3] / "samples"


def _make_cache_probe_pdf(path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page(width=320, height=320)
    page.insert_text((40, 55), "First source paragraph.", fontsize=12)
    page.insert_text((40, 115), "Repeated source paragraph.", fontsize=12)
    page.insert_text((40, 175), "Middle source paragraph.", fontsize=12)
    page.insert_text((40, 235), "Repeated source paragraph.", fontsize=12)
    document.save(path)
    document.close()


def _pdf_render_snapshot(path: Path) -> tuple[tuple[object, ...], ...]:
    """Capture semantic text boxes and exact rendered pixels, ignoring PDF IDs."""
    pages: list[tuple[object, ...]] = []
    with pymupdf.open(path) as document:
        for page in document:
            text_boxes = tuple(
                (
                    tuple(round(float(value), 3) for value in raw[:4]),
                    " ".join(str(raw[4]).split()),
                )
                for raw in page.get_text("blocks")
                if len(raw) > 4 and isinstance(raw[4], str)
            )
            pixmap = page.get_pixmap(alpha=False)
            raster = (pixmap.width, pixmap.height, pixmap.samples)
            pages.append((text_boxes, raster))
    return tuple(pages)


async def test_cache_lookup_maps_source_hashes_to_current_pdf_block_ids(
    tmp_path: Path,
) -> None:
    """Cached translations follow current blocks and their extracted positions."""
    source = tmp_path / "cache-layout.pdf"
    _make_cache_probe_pdf(source)
    blocks = (await PdfExtractor().extract(source, "cache-layout-probe")).blocks
    assert [block.source_text for block in blocks] == [
        "First source paragraph.",
        "Repeated source paragraph.",
        "Middle source paragraph.",
        "Repeated source paragraph.",
    ]

    plan = TranslationPlan(source_language="en", domain="business", register="neutral")
    glossary = {"First": "Erste"}
    key = translation_key("de", "probe-model", "v1", glossary, plan)
    wrong_language_key = translation_key("fr", "probe-model", "v1", glossary, plan)
    assert key != wrong_language_key
    translations_by_source = {
        "First source paragraph.": "Erster Absatz.",
        "Repeated source paragraph.": "Wiederholter Absatz.",
        "Middle source paragraph.": "Mittlerer Absatz.",
    }
    expected = {block.id: translations_by_source[block.source_text] for block in blocks}

    connection = await SqliteConnectionFactory(tmp_path / "cache-probe.db").create()
    try:
        cache = SqliteTranslationCacheRepository(connection)
        async with transaction(connection):
            for block in blocks:
                await cache.save_block_translation(
                    key, block.source_hash, translations_by_source[block.source_text]
                )
                await cache.save_block_translation(
                    wrong_language_key,
                    block.source_hash,
                    f"DECOY {block.source_text}",
                )

        from_cache: dict[str, str] = {}
        for block in blocks:
            translation = await cache.get_block_translation(key, block.source_hash)
            if translation is not None:
                from_cache[block.id] = translation

        # The oracle comes from explicit source-text fixtures, not from copying
        # the cache-derived map; duplicate source text must map to both IDs.
        assert from_cache == expected
        repeated_ids = [
            block.id for block in blocks if block.source_text == "Repeated source paragraph."
        ]
        assert len(repeated_ids) == 2
        assert from_cache[repeated_ids[0]] == from_cache[repeated_ids[1]]

        cached_output = tmp_path / "from-cache.pdf"
        direct_output = tmp_path / "from-fixtures.pdf"
        cached_result = await PdfRenderer().render(source, blocks, from_cache, cached_output)
        direct_result = await PdfRenderer().render(source, blocks, expected, direct_output)
        assert _pdf_render_snapshot(cached_output) == _pdf_render_snapshot(direct_output)
        assert (
            cached_result.degraded_block_ids,
            cached_result.fallback_blocks,
            cached_result.fallback_pages,
        ) == (
            direct_result.degraded_block_ids,
            direct_result.fallback_blocks,
            direct_result.fallback_pages,
        )

        with pymupdf.open(cached_output) as rendered:
            for block in blocks:
                page_number = block.format_metadata["page"]
                source_box = pymupdf.Rect(block.format_metadata["bbox"])
                translated = expected[block.id]
                output_boxes = [
                    pymupdf.Rect(raw[:4])
                    for raw in rendered[page_number].get_text("blocks")
                    if len(raw) > 4
                    and isinstance(raw[4], str)
                    and translated in " ".join(raw[4].split())
                ]
                matching_boxes = [
                    output_box
                    for output_box in output_boxes
                    if not (output_box & source_box).is_empty
                ]
                assert len(matching_boxes) == 1
    finally:
        await connection.close()


@pytest.mark.parametrize("filename", ["platon-gliph.pdf", "platon-gliph.docx"])
async def test_format_metadata_survives_sqlite_roundtrip(tmp_path: Path, filename: str) -> None:
    source = SAMPLES / filename
    document_format = source.suffix.removeprefix(".")
    if document_format == "pdf":
        extracted = await PdfExtractor().extract(source, "layout-persistence-probe")
    else:
        extracted = await DocxExtractor().extract(source, "layout-persistence-probe")

    connection = await SqliteConnectionFactory(tmp_path / f"{document_format}-metadata.db").create()
    try:
        repository = SqliteDocumentRepository(connection)
        async with transaction(connection):
            await repository.create_document(
                extracted.id,
                extracted.filename,
                extracted.format,
                extracted.size_bytes,
                str(source),
                extracted.page_count,
            )
            await repository.create_blocks(extracted.id, extracted.blocks)

        restored = await repository.get_blocks(extracted.id)
        assert [block.format_metadata for block in restored] == [
            block.format_metadata for block in extracted.blocks
        ]
        assert len(restored) == len(extracted.blocks)

        if document_format == "pdf":
            with pymupdf.open(source) as pdf:
                assert len(restored) > 0
                for block in restored:
                    metadata = block.format_metadata
                    page_number = metadata["page"]
                    bbox = metadata["bbox"]
                    assert type(page_number) is int
                    assert 0 <= page_number < pdf.page_count
                    assert isinstance(bbox, list) and len(bbox) == 4
                    assert all(type(value) in {int, float} for value in bbox)
                    assert all(math.isfinite(float(value)) for value in bbox)
                    rectangle = pymupdf.Rect(bbox)
                    assert not rectangle.is_empty
                    page_rect = pdf[page_number].rect
                    assert (
                        rectangle.x0 >= page_rect.x0
                        and rectangle.y0 >= page_rect.y0
                        and rectangle.x1 <= page_rect.x1
                        and rectangle.y1 <= page_rect.y1
                    )
        else:
            assert {block.format_metadata["container"] for block in restored} == {
                "body",
                "table",
            }
    finally:
        await connection.close()
