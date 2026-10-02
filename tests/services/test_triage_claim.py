"""Shared triage ownership against independent connections and processes."""

import asyncio
import sys
from pathlib import Path

import pytest

from app.adapters.llm.triage_runtime import prepare_triage
from app.adapters.persistence.database import SqliteConnectionFactory, transaction
from app.adapters.persistence.repositories import SqliteDocumentRepository
from app.config import Settings
from app.core.models import Block, DocumentIR, DocumentStatus, TranslationPlan


class ControlledAgent:
    def __init__(self) -> None:
        self.calls = 0
        self.closed = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def analyze(self, document: DocumentIR) -> TranslationPlan:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return TranslationPlan(source_language="en", domain="general", register="neutral")

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
async def settings(tmp_path: Path) -> Settings:
    settings = Settings(
        database_path=tmp_path / "shared.db",
        upload_storage_path=tmp_path / "uploads",
        output_storage_path=tmp_path / "output",
        llm_provider="fake",
    )
    connection = await SqliteConnectionFactory(settings.database_path).create()
    try:
        repository = SqliteDocumentRepository(connection)
        async with transaction(connection):
            await repository.create_document("shared", "shared.pdf", "pdf", 1, "/unused")
            await repository.create_blocks(
                "shared",
                [Block(id="block", seq=0, source_text="Hello", source_hash="hash")],
            )
            await repository.update_document_status("shared", DocumentStatus.ANALYZING)
    finally:
        await connection.close()
    return settings


async def test_independent_schedulers_claim_once_and_close_connections_before_agent(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = SqliteConnectionFactory.create
    connections = []

    async def tracked_create(factory):
        connection = await original(factory)
        connections.append(connection)
        return connection

    monkeypatch.setattr(SqliteConnectionFactory, "create", tracked_create)
    agent = ControlledAgent()
    first, second = await asyncio.gather(
        prepare_triage("shared", settings, lambda _: agent),
        prepare_triage("shared", settings, lambda _: agent),
    )
    assert (first is None) != (second is None)
    claim = first if first is not None else second
    task = asyncio.create_task(claim.run())
    try:
        await asyncio.wait_for(agent.started.wait(), 3)
        assert connections
        assert all(connection._connection is None for connection in connections)
        assert await prepare_triage("shared", settings, lambda _: agent) is None
        agent.release.set()
        await asyncio.wait_for(task, 3)
        assert agent.calls == 1
        assert agent.closed
        assert await prepare_triage("shared", settings, lambda _: agent) is None
    finally:
        agent.release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_cancellation_releases_claim_and_restart_recovers(settings: Settings) -> None:
    agent = ControlledAgent()
    claim = await prepare_triage("shared", settings, lambda _: agent)
    assert claim is not None
    task = asyncio.create_task(claim.run())
    await asyncio.wait_for(agent.started.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert agent.closed
    recovered_agent = ControlledAgent()
    recovered_agent.release.set()
    recovered = await prepare_triage("shared", settings, lambda _: recovered_agent)
    assert recovered is not None
    await recovered.run()
    connection = await SqliteConnectionFactory(settings.database_path).create()
    try:
        repository = SqliteDocumentRepository(connection)
        assert (await repository.get_document("shared")).status is DocumentStatus.EXTRACTED
        assert await repository.get_analysis("shared") is not None
    finally:
        await connection.close()


_CHILD = r"""
import asyncio
import sys
from pathlib import Path
from app.adapters.llm.triage_runtime import prepare_triage
from app.config import Settings
from app.core.models import TranslationPlan

class Agent:
    async def analyze(self, document):
        def record():
            with Path(sys.argv[2]).open('a') as stream:
                stream.write('call\n')
        await asyncio.to_thread(record)
        print('provider', flush=True)
        while not await asyncio.to_thread(Path(sys.argv[3]).exists):
            await asyncio.sleep(0.01)
        return TranslationPlan(source_language='en', domain='general', register='neutral')

async def main():
    settings = Settings(database_path=Path(sys.argv[1]), llm_provider='fake')
    claim = await prepare_triage('shared', settings, lambda _: Agent())
    print('claimed' if claim else 'busy', flush=True)
    if claim:
        await claim.run()

asyncio.run(main())
"""


async def _process(settings: Settings, log: Path, release: Path):
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _CHILD,
        str(settings.database_path),
        str(log),
        str(release),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )


async def test_separate_processes_invoke_provider_once(settings: Settings, tmp_path: Path) -> None:
    log, release = tmp_path / "calls", tmp_path / "release"
    first = await _process(settings, log, release)
    second = None
    try:
        assert await asyncio.wait_for(first.stdout.readline(), 15) == b"claimed\n"
        assert await asyncio.wait_for(first.stdout.readline(), 5) == b"provider\n"
        second = await _process(settings, log, release)
        output, _ = await asyncio.wait_for(second.communicate(), 15)
        assert second.returncode == 0
        assert output == b"busy\n"
        await asyncio.to_thread(release.touch)
        await asyncio.wait_for(first.communicate(), 5)
        assert first.returncode == 0
        assert await asyncio.to_thread(log.read_text) == "call\n"
    finally:
        for process in (first, second):
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()


async def test_killed_process_releases_lock_and_analyzing_can_resume(
    settings: Settings, tmp_path: Path
) -> None:
    log, release = tmp_path / "calls", tmp_path / "release"
    first = await _process(settings, log, release)
    recovered = None
    try:
        assert await asyncio.wait_for(first.stdout.readline(), 15) == b"claimed\n"
        assert await asyncio.wait_for(first.stdout.readline(), 5) == b"provider\n"
        first.kill()
        await first.wait()
        await asyncio.to_thread(release.touch)
        recovered = await _process(settings, log, release)
        output, _ = await asyncio.wait_for(recovered.communicate(), 15)
        assert recovered.returncode == 0
        assert output.startswith(b"claimed\nprovider\n")
        # Interrupted calls are ambiguous: recovery is deliberately at least once.
        assert await asyncio.to_thread(log.read_text) == "call\ncall\n"
    finally:
        for process in (first, recovered):
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()


async def test_successful_analysis_repairs_stuck_status_without_provider(
    settings: Settings,
) -> None:
    connection = await SqliteConnectionFactory(settings.database_path).create()
    try:
        repository = SqliteDocumentRepository(connection)
        async with transaction(connection):
            original = await repository.save_analysis(
                "shared",
                TranslationPlan(source_language="en", domain="general", register="neutral"),
            )
        agent = ControlledAgent()
        assert await prepare_triage("shared", settings, lambda _: agent) is None
        assert agent.calls == 0
        assert (await repository.get_document("shared")).status is DocumentStatus.EXTRACTED
        assert await repository.get_analysis("shared") == original
    finally:
        await connection.close()
