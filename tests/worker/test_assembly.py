import asyncio
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from app.adapters.formats.pdf import PdfExtractor, PdfRenderer
from app.adapters.formats.registry import FormatRegistry
from app.adapters.persistence.worker import LeaseLostError
from app.core.errors import DocumentError, ErrorCode
from app.core.models import (
    Block,
    DocumentRecord,
    DocumentStatus,
    JobRecord,
    JobStatus,
    RenderResult,
)
from app.worker.assembly import Assembly


def _job() -> JobRecord:
    now = datetime.now(UTC)
    return JobRecord(
        id="job-1",
        document_id="document-1",
        batch_id="batch-1",
        target_language="German",
        status=JobStatus.RUNNING,
        total_chunks=1,
        done_chunks=1,
        model="gpt-4o-mini",
        prompt_version="v1",
        glossary={},
        tokens_in=0,
        tokens_out=0,
        cost_usd=0.0,
        error_code=None,
        error_detail=None,
        idempotency_key="request-1",
        lease_owner="worker-1",
        lease_expires_at=now,
        created_at=now,
        updated_at=now,
    )


def _document() -> DocumentRecord:
    return DocumentRecord(
        id="document-1",
        filename="source.pdf",
        format="pdf",
        size_bytes=10,
        page_count=1,
        storage_path="/uploads/document-1/source.pdf",
        status=DocumentStatus.EXTRACTED,
        error_code=None,
        created_at=datetime.now(UTC),
    )


class FakePersistence:
    def __init__(self) -> None:
        self.mode: str | None = None
        self.ownership_checks: list[str] = []
        self.fail_next_check = False

    @asynccontextmanager
    async def read(self) -> AsyncIterator[None]:
        assert self.mode is None
        self.mode = "read"
        try:
            yield
        finally:
            self.mode = None

    @asynccontextmanager
    async def write(self) -> AsyncIterator[None]:
        assert self.mode is None
        self.mode = "write"
        try:
            yield
        finally:
            self.mode = None

    async def ensure_owned(self, job_id: str, worker_id: str) -> None:
        assert self.mode in {"read", "write"}
        self.ownership_checks.append(self.mode)
        if self.fail_next_check:
            self.fail_next_check = False
            raise LeaseLostError("job execution lease lost")


class FakeDocumentRepository:
    def __init__(self, blocks: list[Block]) -> None:
        self.blocks = blocks

    async def get_document(self, document_id: str) -> DocumentRecord:
        return _document()

    async def get_blocks(self, document_id: str) -> list[Block]:
        return self.blocks


class FakeCacheRepository:
    def __init__(self, translations: dict[str, str]) -> None:
        self.translations = translations
        self.lookups: list[str] = []

    async def get_block_translation(self, translation_key: str, block_id: str) -> str | None:
        self.lookups.append(block_id)
        return self.translations.get(block_id)


class FakeJobRepository:
    def __init__(self, persistence: FakePersistence) -> None:
        self.persistence = persistence
        self.completed: list[tuple[str, JobStatus]] = []

    async def complete_job(self, job_id: str, status: JobStatus) -> None:
        assert self.persistence.mode == "write"
        self.completed.append((job_id, status))


class FakeStorage:
    def __init__(self, original_path: Path, persistence: FakePersistence) -> None:
        self.original_path = original_path
        self.persistence = persistence
        self.saved: list[tuple[str, bytes, str]] = []

    async def get_upload_path(self, document_id: str) -> Path:
        return self.original_path

    async def save_output(self, job_id: str, content: bytes, filename: str) -> Path:
        assert self.persistence.mode is None
        self.saved.append((job_id, content, filename))
        return self.original_path.parent / "output" / filename


class FakeRenderer:
    def __init__(self, persistence: FakePersistence) -> None:
        self.persistence = persistence
        self.translations: dict[str, str] | None = None
        self.degraded_block_ids: list[str] = []
        self.passthrough_block_ids: list[str] = []
        self.result_path: Path | None = None

    async def render(
        self,
        original_path: Path,
        blocks: list[Block],
        translations: dict[str, str],
        output_path: Path,
    ) -> RenderResult:
        assert self.persistence.mode is None
        self.translations = translations
        rendered_path = self.result_path or output_path
        await asyncio.to_thread(rendered_path.write_bytes, b"rendered document")
        return RenderResult(
            output_path=rendered_path,
            degraded_block_ids=self.degraded_block_ids,
            passthrough_block_ids=self.passthrough_block_ids,
        )


class FakeFormatRegistry:
    def __init__(self, renderer: FakeRenderer) -> None:
        self.renderer = renderer

    async def resolve(self, file_path: Path) -> tuple[object, FakeRenderer]:
        return object(), self.renderer


def _blocks() -> list[Block]:
    return [
        Block(id="block-1", seq=0, source_text="Hello", source_hash="source-1"),
        Block(id="block-2", seq=1, source_text="World", source_hash="source-2"),
    ]


def _assembly(
    tmp_path: Path,
    translations: dict[str, str],
    persistence: FakePersistence | None = None,
) -> tuple[Assembly, FakeRenderer, FakeStorage, FakeJobRepository, FakeCacheRepository]:
    persistence = persistence or FakePersistence()
    renderer = FakeRenderer(persistence)
    storage = FakeStorage(tmp_path / "source.pdf", persistence)
    job_repo = FakeJobRepository(persistence)
    cache_repo = FakeCacheRepository(translations)
    assembly = Assembly(
        job_repo,
        cache_repo,
        FakeDocumentRepository(_blocks()),
        FakeFormatRegistry(renderer),
        storage,
        persistence=persistence,
    )
    return assembly, renderer, storage, job_repo, cache_repo


@pytest.mark.asyncio
async def test_assembly_uses_cache_and_completes_job_done(tmp_path: Path) -> None:
    assembly, renderer, storage, job_repo, cache_repo = _assembly(
        tmp_path,
        {"block-1": "Hallo", "block-2": "Welt"},
    )

    status = await assembly.render(_job())

    assert status is JobStatus.DONE
    assert renderer.translations == {"block-1": "Hallo", "block-2": "Welt"}
    assert cache_repo.lookups == ["block-1", "block-2"]
    assert storage.saved == [("job-1", b"rendered document", "source.pdf")]
    assert job_repo.completed == [("job-1", JobStatus.DONE)]


@pytest.mark.asyncio
async def test_assembly_keeps_missing_blocks_as_source_and_marks_partial(
    tmp_path: Path,
) -> None:
    assembly, renderer, storage, job_repo, _ = _assembly(tmp_path, {"block-1": "Hallo"})

    status = await assembly.render(_job())

    assert status is JobStatus.COMPLETED_WITH_ERRORS
    assert renderer.translations == {"block-1": "Hallo"}
    assert storage.saved[0][1] == b"rendered document"
    assert job_repo.completed == [("job-1", JobStatus.COMPLETED_WITH_ERRORS)]


async def test_assembly_counts_renderer_passthrough_blocks_as_satisfied(
    tmp_path: Path,
) -> None:
    assembly, renderer, _, job_repo, _ = _assembly(tmp_path, {"block-1": "Hallo"})
    renderer.passthrough_block_ids = ["block-2"]

    status = await assembly.render(_job())

    assert status is JobStatus.DONE
    assert job_repo.completed == [("job-1", JobStatus.DONE)]


@pytest.mark.asyncio
async def test_assembly_checks_lease_before_publishing(tmp_path: Path) -> None:
    persistence = FakePersistence()
    persistence.fail_next_check = True
    assembly, _, storage, job_repo, _ = _assembly(
        tmp_path,
        {"block-1": "Hallo", "block-2": "Welt"},
        persistence,
    )

    with pytest.raises(LeaseLostError):
        await assembly.render(_job())

    assert storage.saved == []
    assert job_repo.completed == []
    assert persistence.ownership_checks == ["read"]


async def test_assembly_marks_complete_cache_as_partial_after_render_degrades(
    tmp_path: Path,
) -> None:
    assembly, renderer, storage, job_repo, _ = _assembly(
        tmp_path, {"block-1": "Hallo", "block-2": "Welt"}
    )
    renderer.degraded_block_ids = ["block-1"]
    with capture_logs() as logs:
        status = await assembly.render(_job())

    assert status is JobStatus.COMPLETED_WITH_ERRORS
    assert job_repo.completed == [("job-1", status)]
    assert storage.saved[0][1] == b"rendered document"
    event = next(log for log in logs if log["event"] == "worker_render_degraded")
    assert event["degraded_block_count"] == 1
    assert event["degraded_block_ids"] == ["block-1"]
    assert event["job_id"] == "job-1"
    assert event["document_id"] == "document-1"


async def test_assembly_reads_the_renderer_result_path(tmp_path: Path) -> None:
    assembly, renderer, storage, _, _ = _assembly(tmp_path, {"block-1": "Hallo", "block-2": "Welt"})
    renderer.result_path = tmp_path / "alternate.pdf"

    assert await assembly.render(_job()) is JobStatus.DONE
    assert storage.saved[0][1] == b"rendered document"


@pytest.mark.parametrize("stage", ["render", "save_output"])
async def test_assembly_logs_safe_failure_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    source_text = "PRIVATE SOURCE TEXT"
    translated_text = "PRIVATE TRANSLATED TEXT"
    assembly, renderer, storage, job_repo, _ = _assembly(tmp_path, {"block-1": translated_text})

    async def fail(*args, **kwargs):
        raise ValueError(f"{source_text}: {translated_text}")

    monkeypatch.setattr(renderer if stage == "render" else storage, stage, fail)
    with capture_logs() as logs, pytest.raises(DocumentError) as error:
        await assembly.render(_job())

    assert error.value.error_code is ErrorCode.RENDER_FAILED
    assert job_repo.completed == []
    event = next(log for log in logs if log["event"] == "worker_assembly_failed")
    assert event["stage"] == stage
    assert event["error_type"] == "ValueError"
    assert event["job_id"] == "job-1"
    assert event["document_id"] == "document-1"
    assert source_text not in str(logs)
    assert translated_text not in str(logs)
    assert "exc_info" not in event


@pytest.mark.parametrize(
    ("fixture_name", "expected_status", "degraded_count"),
    [
        ("platon-gliph", JobStatus.COMPLETED_WITH_ERRORS, 1),
        ("platon-complex", JobStatus.DONE, 0),
    ],
)
async def test_assembly_reports_real_pdf_sample_degradation_with_complete_cache(
    tmp_path: Path, fixture_name: str, expected_status: JobStatus, degraded_count: int
) -> None:
    source = Path(__file__).parents[2] / "samples" / f"{fixture_name}.pdf"
    document = await PdfExtractor().extract(source, "document-1")
    translations = {block.id: f"[de] {block.source_text}" for block in document.blocks}
    persistence = FakePersistence()
    storage = FakeStorage(source, persistence)
    job_repo = FakeJobRepository(persistence)
    registry = FormatRegistry()
    registry.register("pdf", PdfExtractor(), PdfRenderer())
    assembly = Assembly(
        job_repo,
        FakeCacheRepository(translations),
        FakeDocumentRepository(document.blocks),
        registry,
        storage,
        persistence=persistence,
    )

    with capture_logs() as logs:
        status = await assembly.render(_job())

    assert status is expected_status
    assert job_repo.completed == [("job-1", expected_status)]
    output = tmp_path / "translated.pdf"
    await asyncio.to_thread(output.write_bytes, storage.saved[0][1])
    rendered_document = await PdfExtractor().extract(output, "output-document")
    assert rendered_document.page_count >= document.page_count
    degradation = [log for log in logs if log["event"] == "worker_render_degraded"]
    if degraded_count:
        assert len(degradation) == 1
        assert degradation[0]["degraded_block_count"] == degraded_count
        degraded_ids = set(degradation[0]["degraded_block_ids"])
        retained_sources = {
            block.source_text for block in document.blocks if block.id in degraded_ids
        }
        output_text = "\n".join(block.source_text for block in rendered_document.blocks)
        for retained_source in retained_sources:
            assert retained_source.splitlines()[0] in output_text
            # Saving a PDF can split an untouched source block into several
            # extraction blocks, so compare glyph counts across the output.
            assert Counter(retained_source.replace("\n", "")) <= Counter(output_text)
    else:
        assert degradation == []
