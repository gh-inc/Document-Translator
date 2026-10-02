"""Generated samples are reproducible and usable through existing format ports."""

import asyncio
from pathlib import Path

import pytest
from docx import Document

from app.adapters.formats.docx import DocxExtractor, DocxRenderer
from app.adapters.formats.pdf import PdfExtractor, PdfRenderer
from app.adapters.formats.registry import FormatRegistry
from app.adapters.llm.fake_provider import FakeProvider
from app.core.errors import DocumentError, ErrorCode
from app.core.models import ChunkRequest, TranslationPlan
from scripts.generate_sample_docs import generate_sample_docs


def _docx_details(path: Path) -> tuple[list[str], list[str], list[list[str]]]:
    document = Document(str(path))
    return (
        [paragraph.text for paragraph in document.paragraphs],
        [paragraph.style.name for paragraph in document.paragraphs],
        [[cell.text for row in table.rows for cell in row.cells] for table in document.tables],
    )


async def test_generated_samples_are_reproducible_and_docx_roundtrips(tmp_path: Path) -> None:
    first = await asyncio.to_thread(generate_sample_docs, tmp_path / "first")
    second = await asyncio.to_thread(generate_sample_docs, tmp_path / "second")
    for source, regenerated in zip(first, second, strict=True):
        assert await asyncio.to_thread(source.read_bytes) == await asyncio.to_thread(
            regenerated.read_bytes
        )

    registry = FormatRegistry()
    registry.register("pdf", PdfExtractor(), PdfRenderer())
    registry.register("docx", DocxExtractor(), DocxRenderer())
    source = first[1]
    adapters = await registry.resolve(source)
    assert adapters is not None
    extractor, renderer = adapters
    document = await extractor.extract(source, "sample-docx")
    assert document.blocks[0].source_text == "Document Translation Sample"
    assert document.blocks[-1].source_text == "Project owner: Alex Morgan. Reference: DT-2401."
    result = await FakeProvider(fail_rate=0, latency_ms=0).translate_chunk(
        ChunkRequest(
            chunk_id="sample-docx-chunk",
            blocks=document.blocks,
            target_language="de",
            plan=TranslationPlan(source_language="en", domain="business", register="neutral"),
            model="gpt-4o-mini",
        )
    )
    output = tmp_path / "translated.docx"
    assert await renderer.render(source, document.blocks, result.translations, output) == output
    _, original_styles, original_tables = await asyncio.to_thread(_docx_details, source)
    texts, styles, tables = await asyncio.to_thread(_docx_details, output)
    assert styles == original_styles
    assert tables == original_tables
    assert len(tables) == 1
    for translation in result.translations.values():
        assert translation in texts


async def test_zip_signature_routes_to_safe_docx_package_validation(tmp_path: Path) -> None:
    malformed = tmp_path / "malformed.docx"
    await asyncio.to_thread(malformed.write_bytes, b"PK\x03\x04not a valid package")
    registry = FormatRegistry()
    registry.register("docx", DocxExtractor(), DocxRenderer())
    adapters = await registry.resolve(malformed)
    assert adapters is not None
    with pytest.raises(DocumentError) as error:
        await adapters[0].extract(malformed, "invalid-document")
    assert error.value.error_code is ErrorCode.CORRUPT_FILE
    assert error.value.retryable is False
