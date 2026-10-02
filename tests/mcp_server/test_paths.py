"""Containment is checked after resolving both input and output symlinks."""

from pathlib import Path

import pytest

from app.mcp_server.paths import (
    ensure_shared_directory,
    require_shared_file,
    resolve_shared_path,
)


def test_relative_and_absolute_files_and_created_output(tmp_path: Path) -> None:
    root = tmp_path / "shared"
    root.mkdir()
    source = root / "input.pdf"
    source.write_bytes(b"sample")
    assert require_shared_file(root, "input.pdf") == source
    assert require_shared_file(root, str(source)) == source
    output = ensure_shared_directory(root, "output/nested")
    assert output == root / "output/nested"
    assert output.is_dir()


@pytest.mark.parametrize("must_exist", [True, False])
def test_traversal_and_absolute_escape(tmp_path: Path, must_exist: bool) -> None:
    root = tmp_path / "shared"
    root.mkdir()
    outside = tmp_path / "private.pdf"
    outside.write_bytes(b"private")
    for supplied in ("../private.pdf", str(outside)):
        with pytest.raises(ValueError, match="outside"):
            resolve_shared_path(root, supplied, must_exist=must_exist)


def test_parent_and_file_symlink_escapes(tmp_path: Path) -> None:
    root = tmp_path / "shared"
    root.mkdir()
    private = tmp_path / "private"
    private.mkdir()
    (private / "secret.pdf").write_bytes(b"private")
    (root / "linked").symlink_to(private, target_is_directory=True)
    (root / "input.pdf").symlink_to(private / "secret.pdf")
    with pytest.raises(ValueError, match="outside"):
        require_shared_file(root, "input.pdf")
    with pytest.raises(ValueError, match="outside"):
        ensure_shared_directory(root, "linked/new-output")
    assert not (private / "new-output").exists()


def test_missing_and_nonregular_input(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        require_shared_file(tmp_path, "missing.pdf")
    with pytest.raises(ValueError, match="regular file"):
        require_shared_file(tmp_path, ".")
    source = tmp_path / "file"
    source.write_bytes(b"sample")
    with pytest.raises(NotADirectoryError):
        ensure_shared_directory(tmp_path, "file")


def test_internal_symlink_is_allowed_and_sibling_prefix_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "shared"
    root.mkdir()
    source = root / "real.pdf"
    source.write_bytes(b"sample")
    (root / "alias.pdf").symlink_to(source)
    assert require_shared_file(root, "alias.pdf") == source
    sibling = tmp_path / "shared-private"
    sibling.mkdir()
    with pytest.raises(ValueError, match="outside"):
        ensure_shared_directory(root, str(sibling))


def test_shared_root_must_exist_and_be_directory(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        ensure_shared_directory(tmp_path / "missing", "output")
    root = tmp_path / "file"
    root.write_bytes(b"sample")
    with pytest.raises(ValueError, match="not a directory"):
        resolve_shared_path(root, "anything", must_exist=False)
