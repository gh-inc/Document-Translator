from __future__ import annotations

import hashlib
import threading
import uuid
from pathlib import Path

import pymupdf
import pytest
import structlog

from app.adapters.formats.pdf import PdfExtractor, PdfRenderer, _split_unsupported
from app.core.errors import DocumentError, ErrorCode


def _make_pdf(path: Path, entries: list[tuple[tuple[float, float], str]]) -> None:
    document = pymupdf.open()
    page = document.new_page(width=300, height=300)
    for point, text in entries:
        page.insert_text(point, text, fontsize=12)
    document.save(path)
    document.close()


@pytest.mark.asyncio
async def test_pdf_extractor_returns_deterministic_reading_order_blocks(tmp_path: Path) -> None:
    source = tmp_path / "order.pdf"
    _make_pdf(
        source,
        [
            ((140, 130), "Middle block with enough text."),
            ((45, 55), "First block with enough text."),
            ((45, 200), "Last block with enough text."),
        ],
    )
    extractor = PdfExtractor()

    document = await extractor.extract(source, "document-1")
    repeated = await extractor.extract(source, "document-1")

    assert document.format == "pdf"
    assert document.page_count == 1
    assert [block.source_text for block in document.blocks] == [
        "First block with enough text.",
        "Middle block with enough text.",
        "Last block with enough text.",
    ]
    assert [block.seq for block in document.blocks] == [0, 1, 2]
    assert [block.id for block in document.blocks] == [block.id for block in repeated.blocks]
    assert document.blocks[0].id == str(uuid.uuid5(uuid.NAMESPACE_URL, "document-1:0"))
    assert (
        document.blocks[0].source_hash
        == hashlib.sha256(document.blocks[0].source_text.encode("utf-8")).hexdigest()
    )
    assert document.blocks[0].format_metadata["page"] == 0
    assert len(document.blocks[0].format_metadata["bbox"]) == 4


@pytest.mark.asyncio
async def test_pdf_extractor_maps_corrupt_and_scanned_documents_to_safe_errors(
    tmp_path: Path,
) -> None:
    corrupt = tmp_path / "corrupt.pdf"
    corrupt.write_bytes(b"not a PDF")
    blank = tmp_path / "blank.pdf"
    document = pymupdf.open()
    document.new_page()
    document.save(blank)
    document.close()
    short = tmp_path / "short.pdf"
    _make_pdf(short, [((45, 60), "OK")])

    extractor = PdfExtractor()
    with pytest.raises(DocumentError) as corrupt_error:
        await extractor.extract(corrupt, "corrupt")
    with pytest.raises(DocumentError) as scanned_error:
        await extractor.extract(blank, "blank")
    short_document = await extractor.extract(short, "short")

    assert corrupt_error.value.error_code == ErrorCode.CORRUPT_FILE
    assert corrupt_error.value.message == "Document cannot be read or is corrupt"
    assert scanned_error.value.error_code == ErrorCode.SCANNED_PDF
    assert scanned_error.value.message == "PDF has no usable text layer; OCR is unsupported"
    assert [block.source_text for block in short_document.blocks] == ["OK"]


@pytest.mark.asyncio
async def test_pdf_renderer_replaces_only_translated_text_and_preserves_artwork(
    tmp_path: Path,
) -> None:
    source = tmp_path / "original.pdf"
    output = tmp_path / "translated.pdf"
    document = pymupdf.open()
    page = document.new_page(width=320, height=320)
    page.insert_text((45, 60), "Translate this paragraph with enough words.", fontsize=12)
    page.insert_text((45, 120), "Keep this other paragraph exactly as written.", fontsize=12)
    page.draw_rect(pymupdf.Rect(210, 40, 280, 110), color=(0, 0, 1), fill=(0.8, 0.8, 1))
    image = pymupdf.Pixmap(pymupdf.csRGB, (0, 0, 6, 6), 0)
    image.clear_with(0xCC2200)
    image_path = tmp_path / "pixel.png"
    image.save(image_path)
    page.insert_image(pymupdf.Rect(220, 180, 260, 220), filename=str(image_path))
    document.save(source)
    document.close()

    extracted = await PdfExtractor().extract(source, "render-document")
    translated_block = extracted.blocks[0].model_copy(
        update={
            "format_metadata": {
                **extracted.blocks[0].format_metadata,
                "adapter_note": "ignored",
            }
        }
    )
    await PdfRenderer().render(
        source,
        [translated_block, *extracted.blocks[1:]],
        {translated_block.id: "Übersetzter Absatz bleibt innerhalb der Seite."},
        output,
    )

    with pymupdf.open(source) as original, pymupdf.open(output) as rendered:
        original_text = original[0].get_text()
        rendered_text = rendered[0].get_text()
        assert "Translate this paragraph" in original_text
        assert "Translate this paragraph" not in rendered_text
        assert "Übersetzter Absatz" in rendered_text
        assert "Keep this other paragraph exactly as written." in rendered_text
        assert len(rendered[0].get_images()) == len(original[0].get_images()) == 1
        assert len(rendered[0].get_drawings()) == len(original[0].get_drawings()) == 1


@pytest.mark.asyncio
async def test_pdf_renderer_paginates_long_unicode_translation_without_truncation(
    tmp_path: Path,
) -> None:
    source = tmp_path / "short.pdf"
    output = tmp_path / "long-translation.pdf"
    _make_pdf(source, [((45, 60), "A source paragraph long enough to be extracted.")])
    block = (await PdfExtractor().extract(source, "long-render")).blocks[0]
    translation = "Привет мир 世界 " * 1200

    with structlog.testing.capture_logs() as logs:
        await PdfRenderer().render(source, [block], {block.id: translation}, output)

    with pymupdf.open(output) as rendered:
        output_text = " ".join(page.get_text() for page in rendered)
        output_page_count = rendered.page_count
        assert output_page_count > 1
        assert output_text.count("Привет") == 1200
        assert output_text.count("мир") == 1200
        assert output_text.count("世界") == 1200

    completion = next(entry for entry in logs if entry.get("event") == "pdf_render_completed")
    assert completion["fallback_count"] == 1
    assert completion["fallback_pages_count"] == output_page_count - 1


@pytest.mark.asyncio
async def test_pdf_renderer_applies_explicit_empty_translation_but_preserves_missing(
    tmp_path: Path,
) -> None:
    source = tmp_path / "empty-translation.pdf"
    output = tmp_path / "empty-rendered.pdf"
    _make_pdf(
        source,
        [
            ((45, 60), "Remove this source paragraph as an empty translation."),
            ((45, 120), "Preserve this paragraph because it is missing."),
        ],
    )
    blocks = (await PdfExtractor().extract(source, "empty-render")).blocks

    await PdfRenderer().render(source, blocks, {blocks[0].id: ""}, output)

    with pymupdf.open(output) as rendered:
        output_text = rendered[0].get_text()
        assert "Remove this source paragraph" not in output_text
        assert "Preserve this paragraph because it is missing." in output_text


@pytest.mark.asyncio
async def test_pdf_library_calls_run_in_worker_threads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "threaded.pdf"
    output = tmp_path / "threaded-output.pdf"
    _make_pdf(source, [((45, 60), "A paragraph long enough for extraction.")])
    main_thread = threading.get_ident()
    observed_threads: list[int] = []

    original_extract = PdfExtractor._extract_sync

    def extract_on_recorded_thread(file_path: Path, document_id: str):
        observed_threads.append(threading.get_ident())
        return original_extract(file_path, document_id)

    monkeypatch.setattr(
        PdfExtractor,
        "_extract_sync",
        staticmethod(extract_on_recorded_thread),
    )
    document = await PdfExtractor().extract(source, "threaded")

    original_render = PdfRenderer._render_sync

    def render_on_recorded_thread(
        original_path: Path,
        blocks,
        translations,
        output_path: Path,
    ):
        observed_threads.append(threading.get_ident())
        return original_render(original_path, blocks, translations, output_path)

    monkeypatch.setattr(
        PdfRenderer,
        "_render_sync",
        staticmethod(render_on_recorded_thread),
    )
    await PdfRenderer().render(
        source,
        document.blocks,
        {document.blocks[0].id: "A translated paragraph that remains on its page."},
        output,
    )

    assert len(observed_threads) == 2
    assert all(thread_id != main_thread for thread_id in observed_threads)


@pytest.mark.asyncio
async def test_pdf_renderer_rejects_out_of_page_bbox_with_catalogued_error(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pdf"
    output = tmp_path / "output.pdf"
    _make_pdf(source, [((45, 60), "A source paragraph long enough to be extracted.")])
    block = (await PdfExtractor().extract(source, "invalid-bbox")).blocks[0]
    invalid_block = block.model_copy(
        update={"format_metadata": {**block.format_metadata, "bbox": [-10, 0, 100, 100]}}
    )

    with pytest.raises(DocumentError) as error:
        await PdfRenderer().render(source, [invalid_block], {block.id: "Translation"}, output)

    assert error.value.error_code == ErrorCode.RENDER_FAILED


def test_pdf_filter_removes_nonprinting_artifacts_and_reports_visible_unsupported_glyph() -> None:
    cleaned, unsupported = _split_unsupported("Hello\x01\x02\u200d\ufe0f\U000e0100\n\t🏛 world")

    assert cleaned == "Hello\n\t🏛 world"
    assert unsupported == {"🏛"}
    assert _split_unsupported("Привет 世界\n\t")[1] == set()


@pytest.mark.asyncio
async def test_pdf_degraded_block_retains_source_and_other_block_is_translated(
    tmp_path: Path,
) -> None:
    source = tmp_path / "degraded.pdf"
    output = tmp_path / "output.pdf"
    _make_pdf(source, [((45, 60), "Retain this original."), ((45, 150), "Replace this original.")])
    blocks = (await PdfExtractor().extract(source, "degraded")).blocks

    with structlog.testing.capture_logs() as logs:
        result = await PdfRenderer().render(
            source,
            blocks,
            {blocks[0].id: "Secret translated text 🏛", blocks[1].id: "Replaced\x01\ufe0f content."},
            output,
        )

    assert result.output_path == output
    assert result.degraded_block_ids == [blocks[0].id]
    assert result.fallback_blocks == result.fallback_pages == 0
    with pymupdf.open(output) as rendered:
        text = rendered[0].get_text()
        assert "Retain this original." in text
        assert "Replace this original." not in text
        assert "Replaced content." in text
    degraded_log = next(log for log in logs if log["event"] == "pdf_block_degraded")
    assert degraded_log["unsupported_codes"] == ["U+1F3DB"]
    assert "Secret translated text" not in str(logs)
    assert "Retain this original" not in str(logs)


@pytest.mark.asyncio
async def test_pdf_neighboring_redaction_preserves_overlapping_degraded_block(
    tmp_path: Path,
) -> None:
    source = tmp_path / "overlap.pdf"
    output = tmp_path / "output.pdf"
    _make_pdf(source, [((45, 60), "Retain this original."), ((45, 150), "Replace this original.")])
    blocks = (await PdfExtractor().extract(source, "overlap")).blocks
    overlapping = blocks[1].model_copy(
        update={"format_metadata": {"page": 0, "bbox": [0, 0, 300, 200]}}
    )

    result = await PdfRenderer().render(
        source, [blocks[0], overlapping], {blocks[0].id: "🏛", overlapping.id: "Replacement"}, output
    )

    assert result.degraded_block_ids == [blocks[0].id]
    with pymupdf.open(output) as rendered:
        text = rendered[0].get_text()
        assert "Retain this original." in text
        assert "Replace this original." not in text
        assert "Replacement" in text


@pytest.mark.asyncio
async def test_pdf_render_exception_logging_never_includes_document_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "Confidential source and translation fragments"

    def fail(*args):
        raise RuntimeError(secret)

    monkeypatch.setattr(PdfRenderer, "_render_sync", staticmethod(fail))
    with structlog.testing.capture_logs() as logs, pytest.raises(DocumentError) as error:
        await PdfRenderer().render(tmp_path / "source.pdf", [], {}, tmp_path / "output.pdf")

    assert error.value.error_code == ErrorCode.RENDER_FAILED
    assert logs == [
        {
            "event": "pdf_render_failed",
            "stage": "render",
            "error_type": "RuntimeError",
            "log_level": "error",
        }
    ]
    assert secret not in str(logs)
    assert secret not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("metadata", "reason"),
    [
        ({"page": "SECRET", "bbox": [1, 2, 3, 4]}, "page_out_of_range"),
        ({"page": 0, "bbox": "SECRET"}, "bbox_shape"),
        ({"page": 0, "bbox": [1, 2, "SECRET", 4]}, "bbox_shape"),
        ({"page": 0, "bbox": [1, 2, float("nan"), 4]}, "not_finite"),
        ({"page": 0, "bbox": [-1, 2, 3, 4]}, "out_of_bounds"),
    ],
)
async def test_pdf_metadata_diagnostics_only_log_validated_geometry(
    tmp_path: Path, metadata: dict, reason: str
) -> None:
    source = tmp_path / "metadata.pdf"
    _make_pdf(source, [((45, 60), "SECRET source document")])
    block = (await PdfExtractor().extract(source, "metadata")).blocks[0]
    invalid = block.model_copy(update={"format_metadata": metadata})

    with structlog.testing.capture_logs() as logs, pytest.raises(DocumentError):
        await PdfRenderer().render(
            source, [invalid], {block.id: "SECRET translation"}, tmp_path / "out.pdf"
        )

    assert logs[0]["stage"] == "metadata"
    assert logs[0]["reason"] == reason
    assert "SECRET" not in str(logs)
    if reason == "out_of_bounds":
        assert logs[0]["bbox"] == [-1.0, 2.0, 3.0, 4.0]
        assert logs[0]["page_rect"] == [0.0, 0.0, 300.0, 300.0]
    else:
        assert "bbox" not in logs[0]


@pytest.mark.asyncio
async def test_pdf_save_exception_diagnostics_are_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "save.pdf"
    _make_pdf(source, [((45, 60), "Confidential source")])
    blocks = (await PdfExtractor().extract(source, "save")).blocks

    def fail_save(*args, **kwargs):
        raise RuntimeError("Confidential translation text from library error")

    monkeypatch.setattr(pymupdf.Document, "save", fail_save)
    with structlog.testing.capture_logs() as logs, pytest.raises(DocumentError):
        await PdfRenderer().render(source, blocks, {}, tmp_path / "out.pdf")

    assert logs[0]["stage"] == "save"
    assert logs[0]["error_type"] == "RuntimeError"
    assert "Confidential" not in str(logs)
