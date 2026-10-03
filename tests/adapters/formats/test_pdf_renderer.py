"""Measured PDF round trips through the offline translation provider."""

import asyncio
from collections import Counter
from pathlib import Path

import pymupdf
import pytest
from structlog.testing import capture_logs

from app.adapters.formats.pdf import PdfExtractor, PdfRenderer
from app.adapters.llm.fake_provider import FakeProvider
from app.core.models import ChunkRequest, TranslationPlan

SAMPLE = Path(__file__).resolve().parents[3] / "samples" / "sample_en.pdf"


def _read_pdf(path: Path) -> tuple[int, str]:
    with pymupdf.open(path) as document:
        return len(document), "\n".join(page.get_text() for page in document)


@pytest.mark.parametrize("expand", [False, True], ids=["fake-de", "synthetic-expansion"])
async def test_sample_pdf_roundtrip_and_fallback_measurement(tmp_path: Path, expand: bool) -> None:
    document = await PdfExtractor().extract(SAMPLE, "sample-pdf")
    request = ChunkRequest(
        chunk_id="sample-chunk",
        blocks=document.blocks,
        target_language="de",
        plan=TranslationPlan(source_language="en", domain="business", register="neutral"),
        model="gpt-4o-mini",
    )
    result = await FakeProvider(fail_rate=0, latency_ms=0).translate_chunk(request)
    translations = result.translations
    if expand:
        # FakeProvider only prefixes text. Add an explicit synthetic expansion
        # of at least 30% to exercise layout; this is not a German quality test.
        translations = {
            block_id: text + " additional details" * max(1, (len(text) * 3 + 179) // 180)
            for block_id, text in translations.items()
        }
    output = tmp_path / "translated.pdf"
    with capture_logs() as logs:
        rendered = await PdfRenderer().render(SAMPLE, document.blocks, translations, output)
    pages, text = await asyncio.to_thread(_read_pdf, output)
    normalized = " ".join(text.split())
    for translation in translations.values():
        assert " ".join(translation.split()) in normalized
    assert rendered.output_path == output
    assert rendered.degraded_block_ids == []
    assert pages >= document.page_count
    completion = next(entry for entry in logs if entry["event"] == "pdf_render_completed")
    assert pages == document.page_count + completion["fallback_pages_count"]
    assert 0 <= completion["fallback_count"] <= len(document.blocks)
    assert rendered.fallback_blocks == completion["fallback_count"]
    assert rendered.fallback_pages == completion["fallback_pages_count"]
    print(
        f"PDF measurement: expansion={expand}, blocks={len(document.blocks)}, "
        f"fallback_blocks={completion['fallback_count']}, "
        f"fallback_pages={completion['fallback_pages_count']}"
    )


async def test_long_overflow_translation_is_paginated_without_truncation(tmp_path: Path) -> None:
    document = await PdfExtractor().extract(SAMPLE, "long-pdf")
    block = document.blocks[0]
    translation = " ".join(f"word{index}" for index in range(3500))
    output = tmp_path / "overflow.pdf"
    with capture_logs() as logs:
        await PdfRenderer().render(SAMPLE, document.blocks, {block.id: translation}, output)
    pages, text = await asyncio.to_thread(_read_pdf, output)
    assert " ".join(translation.split()) in " ".join(text.split())
    completion = next(entry for entry in logs if entry["event"] == "pdf_render_completed")
    assert completion["fallback_count"] == 1
    assert completion["fallback_pages_count"] >= 2
    assert pages == document.page_count + completion["fallback_pages_count"]
    assert (await PdfExtractor().extract(SAMPLE, "long-pdf")) == document


@pytest.mark.parametrize(
    ("fixture", "degraded_count"), [("platon-gliph.pdf", 1), ("platon-complex.pdf", 0)]
)
async def test_platon_pdf_regression_retains_degraded_original_blocks(
    tmp_path: Path, fixture: str, degraded_count: int
) -> None:
    source = SAMPLE.parent / fixture
    document = await PdfExtractor().extract(source, fixture)
    request = ChunkRequest(
        chunk_id="fixture-chunk",
        blocks=document.blocks,
        target_language="de",
        plan=TranslationPlan(source_language="en", domain="business", register="neutral"),
        model="gpt-4o-mini",
    )
    translated = await FakeProvider(fail_rate=0, latency_ms=0).translate_chunk(request)
    result = await PdfRenderer().render(
        source, document.blocks, translated.translations, tmp_path / fixture
    )

    assert len(result.degraded_block_ids) == degraded_count
    assert document.warnings == PdfExtractor.source_warnings(document.blocks)
    if degraded_count:
        assert "U+1F3DB" in document.warnings[0]
        assert "U+0001" in document.warnings[0]
        assert "🏛" not in str(document.warnings)
    else:
        assert document.warnings == []
    with pymupdf.open(source) as original, pymupdf.open(result.output_path) as rendered:
        for block in document.blocks:
            if block.id not in result.degraded_block_ids:
                continue
            page_number = block.format_metadata["page"]
            # Redaction can split original extraction blocks and rewrite font
            # positioning. Compare retained characters rather than requiring
            # identical span grouping; bbox clips also include neighbors.
            source_block = next(
                raw
                for raw in original[page_number].get_text("blocks")
                if list(raw[:4]) == block.format_metadata["bbox"]
            )
            rendered_text = rendered[page_number].get_text()
            assert "Ключевые идеи" in rendered_text
            assert not (Counter(source_block[4].strip()) - Counter(rendered_text))
