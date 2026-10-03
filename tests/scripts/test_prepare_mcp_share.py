"""The shared-directory doctor reports state and never mutates anything."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "prepare-mcp-share.sh"


def _run(
    shared: Path, *args: str, service_uid: int | None = None
) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    if service_uid is not None:
        environment["SERVICE_UID"] = str(service_uid)
    return subprocess.run(
        [str(SCRIPT), str(shared), *args],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )


def _snapshot(root: Path) -> dict[str, tuple[int, int, str]]:
    return {
        str(path.relative_to(root)): (
            path.stat().st_uid,
            path.stat().st_gid,
            oct(path.stat().st_mode),
        )
        for path in sorted(root.rglob("*"))
    }


def test_reports_a_wrong_owned_output_directory(tmp_path: Path) -> None:
    shared = tmp_path / "mcp-files"
    (shared / "output").mkdir(parents=True)
    (shared / "input").mkdir()
    before = _snapshot(shared)

    result = _run(shared, service_uid=os.getuid() + 1)

    assert result.returncode == 1
    assert "output" in result.stdout
    assert f"uid {os.getuid() + 1}" in result.stdout
    # The simplest remedy must be the one the code already supports.
    assert "sudo rm -rf" in result.stdout
    assert _snapshot(shared) == before


def test_reports_success_when_every_directory_is_usable(tmp_path: Path) -> None:
    shared = tmp_path / "mcp-files"
    (shared / "output").mkdir(parents=True)
    (shared / "input").mkdir()

    result = _run(shared, service_uid=os.getuid())

    assert result.returncode == 0
    assert "sudo rm -rf" not in result.stdout


def test_tolerates_a_missing_output_directory(tmp_path: Path) -> None:
    """A missing output directory is healthy: the service provisions it on first use."""
    shared = tmp_path / "mcp-files"
    (shared / "input").mkdir(parents=True)

    result = _run(shared, service_uid=os.getuid())

    assert result.returncode == 0


def test_help_is_available_and_documents_the_contract(tmp_path: Path) -> None:
    result = _run(tmp_path, "--help")

    assert result.returncode == 0
    assert "never modifies" in result.stdout.lower()


def test_default_directory_is_the_repository_mount() -> None:
    """With no argument the script targets ./mcp-files, the Compose bind source."""
    result = subprocess.run([str(SCRIPT), "--help"], capture_output=True, text=True, check=False)

    assert result.returncode == 0
    assert "SERVICE_UID" in result.stdout
