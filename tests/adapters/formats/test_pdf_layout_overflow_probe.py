"""Deterministic probes for findings measured against the live cache."""

import hashlib
import json
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


async def test_missing_translation_can_erase_overlapping_source_block(
    tmp_path: Path,
) -> None:
    """Record the known gap: absent translations do not protect source from redaction."""
    document = await PdfExtractor().extract(GLIPH, "platon-gliph-cache-miss-probe")
    snapshot = _translations_by_seq()
    translations = _cache_translations(document, snapshot)
    missing = next(block for block in document.blocks if block.seq == 4)
    neighbor = next(block for block in document.blocks if block.seq == 5)
    missing_rectangle = pymupdf.Rect(missing.format_metadata["bbox"])
    neighbor_rectangle = pymupdf.Rect(neighbor.format_metadata["bbox"])
    assert not (missing_rectangle & neighbor_rectangle).is_empty
    translations.pop(missing.id)
    output_path = tmp_path / "missing-overlap.pdf"

    await PdfRenderer().render(GLIPH, document.blocks, translations, output_path)

    with pymupdf.open(output_path) as output:
        rendered_text = "\n".join(page.get_text() for page in output)
    assert "Ключевые идеи" not in rendered_text
    assert "Plato argued" in rendered_text
