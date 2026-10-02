import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.adapters.persistence.worker import LeaseLostError
from app.core.models import (
    Block,
    DocumentRecord,
    DocumentStatus,
    JobRecord,
    JobStatus,
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

    async def render(
        self,
        original_path: Path,
        blocks: list[Block],
        translations: dict[str, str],
        output_path: Path,
    ) -> Path:
        assert self.persistence.mode is None
        self.translations = translations
        await asyncio.to_thread(output_path.write_bytes, b"rendered document")
        return output_path


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
