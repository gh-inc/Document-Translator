from pathlib import Path

import pytest

from app.adapters.storage.filesystem import _TEMP_PREFIX, FilesystemStorage


async def test_upload_and_output_roundtrip(tmp_path: Path) -> None:
    storage = FilesystemStorage(tmp_path / "uploads", tmp_path / "out")

    upload_path = await storage.save_upload("doc-1", b"source", "source.pdf")
    output_path = await storage.save_output("job-1", b"translated", "translated.pdf")

    assert upload_path == tmp_path / "uploads" / "doc-1" / "source.pdf"
    assert upload_path.read_bytes() == b"source"
    assert await storage.get_upload_path("doc-1") == upload_path
    assert output_path == tmp_path / "out" / "job-1" / "translated.pdf"
    assert output_path.read_bytes() == b"translated"
    assert await storage.get_output_path("job-1") == output_path


async def test_saving_same_filename_atomically_replaces_artifact(tmp_path: Path) -> None:
    storage = FilesystemStorage(tmp_path / "uploads", tmp_path / "out")

    path = await storage.save_upload("doc-1", b"first", "source.pdf")
    replaced_path = await storage.save_upload("doc-1", b"second", "source.pdf")

    assert replaced_path == path
    assert path.read_bytes() == b"second"
    assert await storage.get_upload_path("doc-1") == path


async def test_saving_different_filename_for_record_is_rejected(tmp_path: Path) -> None:
    storage = FilesystemStorage(tmp_path / "uploads", tmp_path / "out")
    await storage.save_upload("doc-1", b"first", "source.pdf")

    with pytest.raises(FileExistsError, match="already has a published artifact"):
        await storage.save_upload("doc-1", b"second", "other.pdf")


async def test_get_missing_artifact_raises_file_not_found(tmp_path: Path) -> None:
    storage = FilesystemStorage(tmp_path / "uploads", tmp_path / "out")

    with pytest.raises(FileNotFoundError):
        await storage.get_upload_path("missing")


async def test_get_ignores_temporary_files(tmp_path: Path) -> None:
    storage = FilesystemStorage(tmp_path / "uploads", tmp_path / "out")
    directory = tmp_path / "uploads" / "doc-1"
    directory.mkdir(parents=True)
    (directory / f"{_TEMP_PREFIX}interrupted").write_bytes(b"partial")

    with pytest.raises(FileNotFoundError):
        await storage.get_upload_path("doc-1")

    artifact = await storage.save_upload("doc-1", b"published", "source.pdf")
    (directory / f"{_TEMP_PREFIX}interrupted").write_bytes(b"partial")
    assert await storage.get_upload_path("doc-1") == artifact


@pytest.mark.parametrize(
    "record_id",
    ["", ".", "..", "../outside", "/absolute", "a/b", "a\\b", "bad\x00id"],
)
async def test_upload_rejects_invalid_record_ids(tmp_path: Path, record_id: str) -> None:
    storage = FilesystemStorage(tmp_path / "uploads", tmp_path / "out")

    with pytest.raises(ValueError):
        await storage.save_upload(record_id, b"content", "source.pdf")


@pytest.mark.parametrize(
    "filename",
    ["", ".", "..", "../outside", "/absolute", "a/b", "a\\b", "bad\x00name"],
)
async def test_upload_rejects_invalid_filenames(tmp_path: Path, filename: str) -> None:
    storage = FilesystemStorage(tmp_path / "uploads", tmp_path / "out")

    with pytest.raises(ValueError):
        await storage.save_upload("doc-1", b"content", filename)


async def test_get_rejects_identifier_that_escapes_base(tmp_path: Path) -> None:
    storage = FilesystemStorage(tmp_path / "uploads", tmp_path / "uploads-escape")

    with pytest.raises(ValueError):
        await storage.get_upload_path("../uploads-escape")


async def test_save_and_get_reject_symlinked_record_directory_escape(tmp_path: Path) -> None:
    upload_base = tmp_path / "uploads"
    outside_sibling = tmp_path / "uploads-escape"
    upload_base.mkdir()
    outside_sibling.mkdir()
    (upload_base / "doc-1").symlink_to(outside_sibling, target_is_directory=True)
    storage = FilesystemStorage(upload_base, tmp_path / "out")

    with pytest.raises(ValueError, match="path traversal"):
        await storage.save_upload("doc-1", b"content", "source.pdf")
    with pytest.raises(ValueError, match="path traversal"):
        await storage.get_upload_path("doc-1")
    assert list(outside_sibling.iterdir()) == []


async def test_save_and_get_reject_in_base_record_directory_alias(tmp_path: Path) -> None:
    upload_base = tmp_path / "uploads"
    upload_base.mkdir()
    existing_record = upload_base / "doc-existing"
    existing_record.mkdir()
    (existing_record / "source.pdf").write_bytes(b"existing")
    (upload_base / "doc-alias").symlink_to(existing_record, target_is_directory=True)
    storage = FilesystemStorage(upload_base, tmp_path / "out")

    with pytest.raises(ValueError, match="record directory must not be a symlink"):
        await storage.save_upload("doc-alias", b"replacement", "source.pdf")
    with pytest.raises(ValueError, match="record directory must not be a symlink"):
        await storage.get_upload_path("doc-alias")
    assert (existing_record / "source.pdf").read_bytes() == b"existing"


async def test_save_and_get_reject_symlinked_artifact_escape(tmp_path: Path) -> None:
    upload_base = tmp_path / "uploads"
    outside_sibling = tmp_path / "uploads-escape"
    artifact_dir = upload_base / "doc-1"
    artifact_dir.mkdir(parents=True)
    outside_sibling.mkdir()
    outside_file = outside_sibling / "source.pdf"
    outside_file.write_bytes(b"outside")
    (artifact_dir / "source.pdf").symlink_to(outside_file)
    storage = FilesystemStorage(upload_base, tmp_path / "out")

    with pytest.raises(ValueError, match="path traversal"):
        await storage.save_upload("doc-1", b"replacement", "source.pdf")
    with pytest.raises(ValueError, match="path traversal"):
        await storage.get_upload_path("doc-1")
    assert outside_file.read_bytes() == b"outside"


async def test_save_and_get_reject_in_base_artifact_symlink(tmp_path: Path) -> None:
    upload_base = tmp_path / "uploads"
    artifact_dir = upload_base / "doc-1"
    artifact_dir.mkdir(parents=True)
    linked_artifact = upload_base / "shared.pdf"
    linked_artifact.write_bytes(b"shared")
    (artifact_dir / "source.pdf").symlink_to(linked_artifact)
    storage = FilesystemStorage(upload_base, tmp_path / "out")

    with pytest.raises(ValueError, match="artifact path must not be a symlink"):
        await storage.save_upload("doc-1", b"replacement", "source.pdf")
    with pytest.raises(ValueError, match="published artifact paths must not be symlinks"):
        await storage.get_upload_path("doc-1")
    assert linked_artifact.read_bytes() == b"shared"


async def test_get_rejects_ambiguous_multiple_artifacts(tmp_path: Path) -> None:
    storage = FilesystemStorage(tmp_path / "uploads", tmp_path / "out")
    await storage.save_upload("doc-1", b"first", "source.pdf")
    (tmp_path / "uploads" / "doc-1" / "other.pdf").write_bytes(b"second")

    with pytest.raises(FileExistsError, match="multiple published artifacts"):
        await storage.get_upload_path("doc-1")


async def test_failed_atomic_replace_cleans_temporary_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = FilesystemStorage(tmp_path / "uploads", tmp_path / "out")
    await storage.save_upload("doc-1", b"first", "source.pdf")

    def fail_replace(source: str | Path, destination: str | Path) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr("app.adapters.storage.filesystem.os.replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        await storage.save_upload("doc-1", b"second", "source.pdf")

    files = list((tmp_path / "uploads" / "doc-1").iterdir())
    assert [path.name for path in files] == ["source.pdf"]
    assert not any(path.name.startswith(_TEMP_PREFIX) for path in files)
    assert files[0].read_bytes() == b"first"
