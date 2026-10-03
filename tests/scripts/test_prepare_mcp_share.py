"""The shared-directory doctor reports POSIX access without changing the host."""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "prepare-mcp-share.sh"


def _run(
    shared: Path,
    *,
    service_uid: int = 10001,
    service_gid: int | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["SERVICE_UID"] = str(service_uid)
    environment["SERVICE_GID"] = str(service_uid if service_gid is None else service_gid)
    return subprocess.run(
        [str(SCRIPT), str(shared)],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )


def _share(tmp_path: Path, *, input_mode: int = 0o755, output_mode: int | None = 0o755) -> Path:
    shared = tmp_path / "mcp files [$safe]"
    shared.mkdir()
    (shared / "input").mkdir(mode=input_mode)
    if output_mode is not None:
        (shared / "output").mkdir(mode=output_mode)
    shared.chmod(0o777)
    (shared / "input").chmod(input_mode)
    if output_mode is not None:
        (shared / "output").chmod(output_mode)
    return shared


def _snapshot(root: Path) -> dict[str, tuple[int, int, str]]:
    return {
        str(path.relative_to(root)): (
            path.lstat().st_uid,
            path.lstat().st_gid,
            oct(path.lstat().st_mode),
        )
        for path in sorted(root.rglob("*"))
    }


def test_owner_uid_still_needs_the_requested_permission_bits(tmp_path: Path) -> None:
    """POSIX owner bits take precedence; group/other grants do not rescue them."""
    service_uid = os.getuid()
    shared = _share(tmp_path, output_mode=0o507)
    before = _snapshot(shared)

    result = _run(shared, service_uid=service_uid, service_gid=os.getgid())

    assert result.returncode == 1
    assert "output" in result.stdout
    assert "does not meet required read, write, and search permissions" in result.stdout
    assert "sudo chown" in result.stdout
    assert _snapshot(shared) == before


@pytest.mark.parametrize(
    ("directory", "mode", "diagnostic"),
    [
        ("input", 0o400, "read and search"),
        ("output", 0o300, "read, write, and search"),
    ],
)
def test_directories_need_search_as_well_as_data_access(
    tmp_path: Path, directory: str, mode: int, diagnostic: str
) -> None:
    shared = _share(tmp_path)
    (shared / directory).chmod(mode)

    result = _run(shared, service_uid=os.getuid())

    assert result.returncode == 1
    assert diagnostic in result.stdout


def test_uses_group_bits_for_configured_gid(tmp_path: Path) -> None:
    shared = _share(tmp_path)
    (shared / "input").chmod(0o550)
    (shared / "output").chmod(0o570)
    shared.chmod(0o550)

    result = _run(shared, service_uid=os.getuid() + 1, service_gid=os.getgid())

    assert result.returncode == 0
    assert "all probed directory operations satisfy" in result.stdout


def test_uses_other_bits_when_uid_and_gid_do_not_match(tmp_path: Path) -> None:
    shared = _share(tmp_path, output_mode=0o757)

    result = _run(
        shared,
        service_uid=os.getuid() + 1,
        service_gid=os.getgid() + 1,
    )

    assert result.returncode == 0


def test_missing_output_requires_writable_searchable_parent(tmp_path: Path) -> None:
    shared = _share(tmp_path, output_mode=None)
    shared.chmod(0o555)

    result = _run(shared, service_uid=os.getuid())

    assert result.returncode == 1
    assert "output" in result.stdout
    assert "parent" in result.stdout
    assert "read, write, and search" in result.stdout


def test_missing_output_is_healthy_when_parent_can_create_it(tmp_path: Path) -> None:
    shared = _share(tmp_path, output_mode=None)
    shared.chmod(0o733)

    result = _run(shared, service_uid=os.getuid())

    assert result.returncode == 0
    assert "output: missing" in result.stdout


@pytest.mark.parametrize("kind", ["symlink", "file"])
def test_non_directory_and_symlink_components_are_reported_as_unusable(
    tmp_path: Path, kind: str
) -> None:
    shared = _share(tmp_path)
    output = shared / "output"
    output.rmdir()
    if kind == "symlink":
        outside = tmp_path / "outside"
        outside.mkdir()
        output.symlink_to(outside, target_is_directory=True)
    else:
        output.write_text("not a directory")

    result = _run(shared, service_uid=os.getuid())

    assert result.returncode == 1
    if kind == "symlink":
        assert "output: symlink" in result.stdout
    else:
        assert "output: non-directory" in result.stdout


def test_remedies_quote_actual_paths_and_configured_uid_gid_without_mutating(
    tmp_path: Path,
) -> None:
    shared = _share(tmp_path, output_mode=0o555)
    renamed = tmp_path / "mcp's files [$safe]"
    shared.rename(renamed)
    shared = renamed
    before = _snapshot(shared)
    service_uid = os.getuid() + 1
    service_gid = os.getgid() + 1

    result = _run(shared, service_uid=service_uid, service_gid=service_gid)

    assert result.returncode == 1
    assert str(shared) in shlex.split(result.stdout.splitlines()[0])
    assert f"{service_uid}:{service_gid}" in result.stdout
    chown_line = next(line.strip() for line in result.stdout.splitlines() if "sudo chown " in line)
    assert shlex.split(chown_line) == [
        "sudo",
        "chown",
        f"{service_uid}:{service_gid}",
        str(shared / "output"),
    ]
    remove_line = next(line.strip() for line in result.stdout.splitlines() if "sudo rm -rf" in line)
    assert shlex.split(remove_line) == [
        "sudo",
        "rm",
        "-rf",
        "--",
        str(shared / "output"),
    ]
    assert "permanently deletes existing output artifacts" in result.stdout
    assert _snapshot(shared) == before


def test_acl_and_supplementary_group_access_are_explicitly_out_of_scope(tmp_path: Path) -> None:
    result = subprocess.run([str(SCRIPT), "--help"], capture_output=True, text=True, check=False)

    assert result.returncode == 0
    assert "ACL" in result.stdout
    assert "supplementary groups" in result.stdout
