"""Document uploads persist extracted content and the pending background analysis."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from io import BytesIO
from pathlib import Path
from threading import Event

import pymupdf
import pytest
from fastapi import UploadFile
from pydantic import ValidationError

from app.adapters.formats.docx import DocxExtractor, DocxRenderer
from app.adapters.formats.pdf import PdfExtractor, PdfRenderer
from app.adapters.formats.registry import FormatRegistry
from app.adapters.persistence.database import SqliteConnectionFactory, transaction
from app.adapters.persistence.repositories import SqliteDocumentRepository
from app.adapters.storage.filesystem import FilesystemStorage
from app.api.routers.documents import _read_bounded_upload
from app.config import Settings
from app.core.errors import DocumentError, ErrorCode, ServiceError
from app.core.models import DocumentStatus
from app.core.services.document_service import DocumentService, UploadResult, sanitize_filename

SAMPLES = Path(__file__).resolve().parents[2] / "samples"


def _create_text_pdf(path: Path, page_count: int) -> None:
    document = pymupdf.open()
    for index in range(page_count):
        page = document.new_page()
        if index == 0:
            page.insert_text((72, 72), "Text layer for upload boundary test")
    document.save(path)
    document.close()


@pytest.fixture
async def document_context(
    tmp_path: Path,
) -> AsyncIterator[tuple[DocumentService, SqliteDocumentRepository, FilesystemStorage]]:
    connection = await SqliteConnectionFactory(tmp_path / "api.db").create()
    repository = SqliteDocumentRepository(connection)
    storage = FilesystemStorage(tmp_path / "uploads", tmp_path / "out")
    registry = FormatRegistry()
    registry.register("pdf", PdfExtractor(), PdfRenderer())
    registry.register("docx", DocxExtractor(), DocxRenderer())

    async def cleanup(document_id: str) -> None:
        path = await storage.get_upload_path(document_id)
        await asyncio.to_thread(path.unlink)
        await asyncio.to_thread(path.parent.rmdir)

    service = DocumentService(
        repository,
        storage,
        registry,
        lambda: transaction(connection),
        Settings(
            database_path=tmp_path / "api.db",
            upload_storage_path=tmp_path / "uploads",
            output_storage_path=tmp_path / "out",
        ),
        cleanup,
    )
    try:
        yield service, repository, storage
    finally:
        await connection.close()


@pytest.mark.parametrize("extension", ["pdf", "docx"])
async def test_upload_persists_document_blocks_without_analysis(
    document_context: tuple[DocumentService, SqliteDocumentRepository, FilesystemStorage],
    extension: str,
) -> None:
    service, repository, _storage = document_context
    content = await asyncio.to_thread((SAMPLES / f"sample_en.{extension}").read_bytes)

    result = await service.upload(
        f"C:\\fakepath\\report.{extension}",
        content,
    )

    document = result.document
    block_count = result.block_count
    assert result.warnings == []
    persisted_document = await repository.get_document(document.id)
    blocks = await repository.get_blocks(document.id)
    analysis = await repository.get_analysis(document.id)
    assert document.filename == f"report.{extension}"
    assert document.format == extension
    assert document.status is DocumentStatus.ANALYZING
    assert persisted_document is not None
    assert persisted_document.status is DocumentStatus.ANALYZING
    if extension == "pdf":
        assert persisted_document.page_count is not None
    assert block_count > 0
    assert len(blocks) == block_count
    assert analysis is None


async def test_pdf_upload_and_duplicate_return_ephemeral_safe_warnings(
    document_context: tuple[DocumentService, SqliteDocumentRepository, FilesystemStorage],
) -> None:
    service, repository, _storage = document_context
    content = await asyncio.to_thread((SAMPLES / "platon-gliph.pdf").read_bytes)

    first = await service.upload("platon-gliph.pdf", content)
    blocks = await repository.get_blocks(first.document.id)
    async with service._transaction_context():
        await repository.update_document_status(first.document.id, DocumentStatus.EXTRACTED)
    duplicate = await service.upload("renamed.pdf", content)

    assert first.warnings
    assert duplicate.warnings == first.warnings
    assert duplicate.document.id == first.document.id
    assert duplicate.document.filename == "platon-gliph.pdf"
    assert duplicate.document.status is DocumentStatus.EXTRACTED
    assert duplicate.block_count == first.block_count
    assert await repository.get_blocks(first.document.id) == blocks
    assert await repository.get_analysis(first.document.id) is None
    warning_text = " ".join(first.warnings)
    assert "U+" in warning_text
    assert all(block.source_text not in warning_text for block in blocks)
    assert "warnings" not in duplicate.document.model_dump()


@pytest.mark.parametrize("filename", ["platon-complex.pdf", "platon-gliph.docx"])
async def test_clean_pdf_and_docx_uploads_have_no_warnings_including_duplicates(
    document_context: tuple[DocumentService, SqliteDocumentRepository, FilesystemStorage],
    filename: str,
) -> None:
    service, _repository, _storage = document_context
    content = await asyncio.to_thread((SAMPLES / filename).read_bytes)

    first = await service.upload(filename, content)
    duplicate = await service.upload(filename, content)

    assert first.warnings == duplicate.warnings == []
    assert first.document == duplicate.document
    assert first.block_count == duplicate.block_count


async def test_duplicate_warning_extraction_failure_preserves_existing_upload(
    document_context: tuple[DocumentService, SqliteDocumentRepository, FilesystemStorage],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repository, storage = document_context
    content = await asyncio.to_thread((SAMPLES / "sample_en.pdf").read_bytes)
    first = await service.upload("sample.pdf", content)
    blocks = await repository.get_blocks(first.document.id)

    async def fail_extract(file_path: Path, document_id: str) -> None:
        raise DocumentError(ErrorCode.CORRUPT_FILE)

    monkeypatch.setattr(PdfExtractor, "extract", staticmethod(fail_extract))
    with pytest.raises(DocumentError) as raised:
        await service.upload("sample.pdf", content)

    assert raised.value.error_code is ErrorCode.CORRUPT_FILE
    assert await repository.get_document(first.document.id) == first.document
    assert await repository.get_blocks(first.document.id) == blocks
    original = await storage.get_upload_path(first.document.id)
    assert await asyncio.to_thread(original.read_bytes) == content


async def test_upload_result_rejects_extra_fields_and_isolates_warning_defaults(
    document_context: tuple[DocumentService, SqliteDocumentRepository, FilesystemStorage],
) -> None:
    service, _repository, _storage = document_context
    content = await asyncio.to_thread((SAMPLES / "sample_en.docx").read_bytes)
    result = await service.upload("sample.docx", content)
    value = {"document": result.document, "block_count": result.block_count}
    first = UploadResult.model_validate(value)
    second = UploadResult.model_validate(value)
    first.warnings.append("warning")

    assert second.warnings == []
    with pytest.raises(ValidationError):
        UploadResult.model_validate({**value, "unexpected": True})


def test_filename_sanitizer_uses_safe_basename_for_both_path_styles() -> None:
    assert sanitize_filename(r"C:\fakepath\a report.pdf") == "a report.pdf"
    assert sanitize_filename("../../a report.pdf") == "a report.pdf"
    assert sanitize_filename("unsafe/name?.docx") == "name_.docx"


async def test_router_rejects_oversize_body_after_bounded_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.api.routers.documents.MAX_UPLOAD_BYTES", 5)
    upload = UploadFile(file=BytesIO(b"123456"), filename="large.pdf")

    with pytest.raises(ServiceError) as raised:
        await _read_bounded_upload(upload)

    await upload.close()
    assert raised.value.error_code is ErrorCode.FILE_TOO_LARGE
    assert raised.value.status_code == 413


async def test_unsupported_format_is_catalogued_before_storage(
    document_context: tuple[DocumentService, SqliteDocumentRepository, FilesystemStorage],
) -> None:
    service, _repository, _storage = document_context

    with pytest.raises(ServiceError) as raised:
        await service.upload("notes.txt", b"text")

    assert raised.value.error_code is ErrorCode.UNSUPPORTED_FORMAT
    assert raised.value.status_code == 415


async def test_bad_pdf_signature_is_catalogued_as_corrupt_and_cleaned(
    document_context: tuple[DocumentService, SqliteDocumentRepository, FilesystemStorage],
) -> None:
    service, repository, storage = document_context
    called: list[str] = []
    original_cleanup = service._cleanup_upload

    async def track_cleanup(value: str) -> None:
        called.append(value)
        assert original_cleanup is not None
        await original_cleanup(value)

    service._cleanup_upload = track_cleanup
    with pytest.raises(DocumentError) as raised:
        await service.upload("broken.pdf", b"not a pdf")

    assert raised.value.error_code is ErrorCode.CORRUPT_FILE
    assert len(called) == 1
    assert await repository.get_document(called[0]) is None
    with pytest.raises(FileNotFoundError):
        await storage.get_upload_path(called[0])


async def test_scanned_pdf_failure_does_not_persist_a_partial_document(
    document_context: tuple[DocumentService, SqliteDocumentRepository, FilesystemStorage],
    tmp_path: Path,
) -> None:
    service, repository, _storage = document_context
    scanned_path = tmp_path / "blank.pdf"
    pdf = pymupdf.open()
    pdf.new_page()
    await asyncio.to_thread(pdf.save, scanned_path)
    pdf.close()
    content = await asyncio.to_thread(scanned_path.read_bytes)

    called: list[str] = []
    original_cleanup = service._cleanup_upload

    async def track_cleanup(value: str) -> None:
        called.append(value)
        assert original_cleanup is not None
        await original_cleanup(value)

    service._cleanup_upload = track_cleanup
    with pytest.raises(DocumentError) as raised:
        await service.upload("blank.pdf", content)

    assert raised.value.error_code is ErrorCode.SCANNED_PDF
    assert len(called) == 1
    assert await repository.get_document(called[0]) is None


async def test_400_page_pdf_is_accepted_and_401_page_pdf_is_rejected(
    document_context: tuple[DocumentService, SqliteDocumentRepository, FilesystemStorage],
    tmp_path: Path,
) -> None:
    service, repository, storage = document_context
    accepted_path = tmp_path / "accepted.pdf"
    await asyncio.to_thread(_create_text_pdf, accepted_path, 400)
    accepted_bytes = await asyncio.to_thread(accepted_path.read_bytes)

    result = await service.upload("accepted.pdf", accepted_bytes)
    accepted_document = result.document
    block_count = result.block_count

    assert accepted_document.page_count == 400
    assert block_count == 1
    assert await repository.get_document(accepted_document.id) is not None

    rejected_path = tmp_path / "rejected.pdf"
    await asyncio.to_thread(_create_text_pdf, rejected_path, 401)
    rejected_bytes = await asyncio.to_thread(rejected_path.read_bytes)
    called: list[str] = []
    original_cleanup = service._cleanup_upload

    async def track_cleanup(value: str) -> None:
        called.append(value)
        assert original_cleanup is not None
        await original_cleanup(value)

    service._cleanup_upload = track_cleanup
    with pytest.raises(DocumentError) as raised:
        await service.upload("rejected.pdf", rejected_bytes)

    assert raised.value.error_code is ErrorCode.PAGE_LIMIT
    assert len(called) == 1
    assert await repository.get_document(called[0]) is None
    with pytest.raises(FileNotFoundError):
        await storage.get_upload_path(called[0])


async def test_extracted_text_limit_uses_utf8_bytes_and_cleans_rejected_upload(
    document_context: tuple[DocumentService, SqliteDocumentRepository, FilesystemStorage],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repository, storage = document_context
    sample_path = SAMPLES / "sample_en.docx"
    content = await asyncio.to_thread(sample_path.read_bytes)
    extracted = await DocxExtractor().extract(sample_path, "measure-text")
    extracted_text_bytes = sum(len(block.source_text.encode("utf-8")) for block in extracted.blocks)
    monkeypatch.setattr(
        "app.core.services.document_service.MAX_EXTRACTED_TEXT_BYTES",
        extracted_text_bytes,
    )

    result = await service.upload("accepted.docx", content)
    accepted_document = result.document
    accepted_block_count = result.block_count

    assert accepted_block_count == len(extracted.blocks)
    assert await repository.get_document(accepted_document.id) is not None

    monkeypatch.setattr(
        "app.core.services.document_service.MAX_EXTRACTED_TEXT_BYTES",
        extracted_text_bytes - 1,
    )
    called: list[str] = []
    original_cleanup = service._cleanup_upload

    async def track_cleanup(value: str) -> None:
        called.append(value)
        assert original_cleanup is not None
        await original_cleanup(value)

    service._cleanup_upload = track_cleanup
    with pytest.raises(DocumentError) as raised:
        await service.upload("rejected.docx", content + b"\x00")

    assert raised.value.error_code is ErrorCode.TEXT_LIMIT
    assert len(called) == 1
    assert await repository.get_document(called[0]) is None
    with pytest.raises(FileNotFoundError):
        await storage.get_upload_path(called[0])


async def test_document_and_blocks_roll_back_when_status_write_fails(
    document_context: tuple[DocumentService, SqliteDocumentRepository, FilesystemStorage],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repository, _storage = document_context
    content = await asyncio.to_thread((SAMPLES / "sample_en.docx").read_bytes)
    uploaded_ids: list[str] = []

    async def fail_status(document_id: str, status: object, error_code: str | None = None) -> None:
        uploaded_ids.append(document_id)
        raise RuntimeError("injected persistence failure")

    monkeypatch.setattr(repository, "update_document_status", fail_status)
    with pytest.raises(RuntimeError, match="injected persistence failure"):
        await service.upload("report.docx", content)

    assert len(uploaded_ids) == 1
    assert await repository.get_document(uploaded_ids[0]) is None
    assert await repository.get_blocks(uploaded_ids[0]) == []
    assert await repository.get_analysis(uploaded_ids[0]) is None


async def test_cancellation_waits_for_threaded_upload_then_cleans_it(
    document_context: tuple[DocumentService, SqliteDocumentRepository, FilesystemStorage],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repository, storage = document_context
    content = await asyncio.to_thread((SAMPLES / "sample_en.pdf").read_bytes)
    save_started = Event()
    allow_save_to_finish = Event()
    uploaded_ids: list[str] = []
    original_save = storage._save_artifact

    def delayed_save(base: Path, record_id: str, payload: bytes, filename: str) -> Path:
        uploaded_ids.append(record_id)
        save_started.set()
        if not allow_save_to_finish.wait(timeout=5):
            raise TimeoutError("test did not release delayed upload save")
        return original_save(base, record_id, payload, filename)

    monkeypatch.setattr(storage, "_save_artifact", delayed_save)
    upload_task = asyncio.create_task(service.upload("delayed.pdf", content))
    try:
        assert await asyncio.to_thread(save_started.wait, 3)
        upload_task.cancel()
        await asyncio.sleep(0)
    finally:
        allow_save_to_finish.set()
        if not upload_task.done():
            upload_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await upload_task

    assert len(uploaded_ids) == 1
    assert await repository.get_document(uploaded_ids[0]) is None
    with pytest.raises(FileNotFoundError):
        await storage.get_upload_path(uploaded_ids[0])
