"""Deterministic probes for findings measured against the live cache."""

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pymupdf

from app.adapters.formats.pdf import PdfExtractor, PdfRenderer

SAMPLES = Path(__file__).resolve().parents[3] / "samples"
GLIPH = SAMPLES / "platon-gliph.pdf"
CACHE_SNAPSHOT = Path(__file__).parent / "fixtures" / "platon-gliph-en-cache-snapshot.json"


def _translations_by_seq() -> dict[str, Any]:
    return json.loads(CACHE_SNAPSHOT.read_text(encoding="utf-8"))


def _cache_translations(document: Any, snapshot: dict[str, Any]) -> dict[str, str]:
    provenance = snapshot["provenance"]
    assert provenance["source_document_id"] == hashlib.sha256(GLIPH.read_bytes()).hexdigest()
    assert provenance["source_filename"] == GLIPH.name
    return {
        block.id: snapshot["translations_by_seq"][str(block.seq)]
        for block in document.blocks
        if str(block.seq) in snapshot["translations_by_seq"]
    }


async def test_live_cache_baseline_has_42_fallbacks_and_one_degraded_block(
    tmp_path: Path,
) -> None:
    """Pin the measured 70-block cache result, including DT-77 degradation."""
    document = await PdfExtractor().extract(GLIPH, "platon-gliph-live-cache-probe")
    snapshot = _translations_by_seq()
    translations = _cache_translations(document, snapshot)

    assert snapshot["provenance"]["model"] == "gpt-4o-mini"
    assert snapshot["provenance"]["translation_key_prefix"] == "5531d8d1d297"
    assert len(document.blocks) == len(translations) == 70
    output_path = tmp_path / "live-cache.pdf"
    result = await PdfRenderer().render(GLIPH, document.blocks, translations, output_path)

    degraded = [block.seq for block in document.blocks if block.id in result.degraded_block_ids]
    assert degraded == [4]
    assert result.fallback_blocks == 42
    assert result.fallback_pages == 42
    with pymupdf.open(output_path) as output:
        rendered_text = "\n".join(page.get_text() for page in output)
        assert len(output) == 45
    assert "Ключевые идеи" in rendered_text
    assert "Plato argued" in rendered_text


def _longest_word(text: str, alphabet: str) -> str:
    words = re.findall(rf"[{alphabet}]{{6,}}", text)
    assert words, text
    return max(words, key=len)


CYRILLIC = r"Ѐ-ӿ"
LATIN = r"A-Za-z"


async def test_missing_translation_preserves_overlapping_source_block(
    tmp_path: Path,
) -> None:
    """DT-108: an absent translation must not let a neighbour erase source text."""
    document = await PdfExtractor().extract(GLIPH, "platon-gliph-cache-miss-probe")
    snapshot = _translations_by_seq()
    translations = _cache_translations(document, snapshot)
    missing = next(block for block in document.blocks if block.seq == 4)
    neighbor = next(block for block in document.blocks if block.seq == 5)
    missing_rectangle = pymupdf.Rect(missing.format_metadata["bbox"])
    neighbor_rectangle = pymupdf.Rect(neighbor.format_metadata["bbox"])
    assert not (missing_rectangle & neighbor_rectangle).is_empty
    neighbor_translation = translations[neighbor.id]
    translations.pop(missing.id)
    output_path = tmp_path / "missing-overlap.pdf"

    await PdfRenderer().render(GLIPH, document.blocks, translations, output_path)

    with pymupdf.open(output_path) as output:
        rendered_text = "\n".join(page.get_text() for page in output)
    # The untranslated block keeps its source even though a neighbour overlaps it.
    assert _longest_word(missing.source_text, CYRILLIC) in rendered_text
    # Redaction still works: the neighbour's own source is gone, its text is not.
    assert _longest_word(neighbor.source_text, CYRILLIC) not in rendered_text
    assert _longest_word(neighbor_translation, LATIN) in rendered_text


async def test_nested_source_block_survives_neighbour_redaction(
    tmp_path: Path,
) -> None:
    """DT-108: containment, where no constant inset could help."""
    document = await PdfExtractor().extract(GLIPH, "platon-gliph-nested-probe")
    snapshot = _translations_by_seq()
    translations = _cache_translations(document, snapshot)
    nested = next(block for block in document.blocks if block.seq == 26)
    tall = next(block for block in document.blocks if block.seq == 27)
    nested_rectangle = pymupdf.Rect(nested.format_metadata["bbox"])
    tall_rectangle = pymupdf.Rect(tall.format_metadata["bbox"])
    intersection = nested_rectangle & tall_rectangle
    assert not intersection.is_empty
    assert abs(intersection.height - nested_rectangle.height) < 0.5
    assert intersection.width < nested_rectangle.width
    tall_translation = translations[tall.id]
    translations.pop(nested.id)
    output_path = tmp_path / "nested-overlap.pdf"

    await PdfRenderer().render(GLIPH, document.blocks, translations, output_path)

    with pymupdf.open(output_path) as output:
        rendered_text = "\n".join(page.get_text() for page in output)
    assert _longest_word(nested.source_text, CYRILLIC) in rendered_text
    assert _longest_word(tall.source_text, CYRILLIC) not in rendered_text
    assert _longest_word(tall_translation, LATIN) in rendered_text


async def test_block_intersection_geometry_is_unchanged() -> None:
    """Pin the overlap measurements that made shrink-based protection unusable."""
    document = await PdfExtractor().extract(GLIPH, "platon-gliph-intersection-geometry")
    rectangles = [pymupdf.Rect(block.format_metadata["bbox"]) for block in document.blocks]
    heights = []
    intersecting = 0
    for own, following in zip(document.blocks, document.blocks[1:], strict=False):
        if own.format_metadata["page"] != following.format_metadata["page"]:
            continue
        intersection = rectangles[own.seq] & rectangles[following.seq]
        if intersection.is_empty:
            continue
        intersecting += 1
        heights.append(intersection.height)
    assert intersecting == 49
    assert round(sorted(heights)[len(heights) // 2], 2) == 2.54
    assert round(max(heights), 2) == 16.34
