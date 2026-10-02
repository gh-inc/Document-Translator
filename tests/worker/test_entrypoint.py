"""Worker startup cleanup and process-level graceful shutdown checks."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path

import pytest

from app.config import Settings
from app.worker import __main__ as worker_entrypoint


class _ConnectionStub:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


async def test_run_worker_selects_fake_provider_and_closes_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _ConnectionStub()
    captured: dict[str, object] = {}

    async def create_connection(self):
        return connection

    class ClaimLoopStub:
        def __init__(self, *args, **kwargs) -> None:
            captured["provider"] = args[4]
            captured["shutdown_event"] = kwargs["shutdown_event"]

        async def run(self) -> None:
            captured["ran"] = True

    monkeypatch.setattr(worker_entrypoint.SqliteConnectionFactory, "create", create_connection)
    monkeypatch.setattr(worker_entrypoint, "ClaimLoop", ClaimLoopStub)
    shutdown = asyncio.Event()
    await worker_entrypoint.run_worker(
        Settings(
            database_path=tmp_path / "entrypoint.db",
            upload_storage_path=tmp_path / "uploads",
            output_storage_path=tmp_path / "outputs",
            llm_provider="fake",
        ),
        shutdown_event=shutdown,
    )

    from app.adapters.llm.fake_provider import FakeProvider

    assert isinstance(captured["provider"], FakeProvider)
    assert captured["shutdown_event"] is shutdown
    assert captured["ran"] is True
    assert connection.closed


async def test_run_worker_closes_connection_when_provider_construction_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _ConnectionStub()

    async def create_connection(self):
        return connection

    def fail_provider(*args, **kwargs):
        raise RuntimeError("provider setup failed")

    monkeypatch.setattr(worker_entrypoint.SqliteConnectionFactory, "create", create_connection)
    monkeypatch.setattr(worker_entrypoint, "FakeProvider", fail_provider)
    with pytest.raises(RuntimeError, match="provider setup failed"):
        await worker_entrypoint.run_worker(
            Settings(
                database_path=tmp_path / "entrypoint.db",
                llm_provider="fake",
            ),
            shutdown_event=asyncio.Event(),
        )
    assert connection.closed


async def test_worker_module_idles_and_exits_after_sigterm(tmp_path: Path) -> None:
    repository_root = Path(__file__).parents[2]
    environment = os.environ.copy()
    environment.update(
        {
            "DATABASE_PATH": str(tmp_path / "worker.db"),
            "UPLOAD_STORAGE_PATH": str(tmp_path / "uploads"),
            "OUTPUT_STORAGE_PATH": str(tmp_path / "outputs"),
            "LLM_PROVIDER": "fake",
            "HEARTBEAT_INTERVAL_SECONDS": "1",
            "JOB_LEASE_SECONDS": "3",
            "CHUNK_LEASE_SECONDS": "3",
        }
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "app.worker",
        cwd=repository_root,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert process.stdout is not None
        readiness_line = await asyncio.wait_for(process.stdout.readline(), timeout=10)
        readiness = json.loads(readiness_line)
        assert readiness["event"] == "worker_started"
        process.send_signal(signal.SIGTERM)
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
        assert process.returncode == 0, stdout.decode("utf-8", errors="replace") + stderr.decode(
            "utf-8", errors="replace"
        )
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
