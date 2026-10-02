import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pymupdf
import pytest

from app.config import Settings
from app.mcp_server.runtime import McpRuntime


@pytest.fixture
def mcp_settings(tmp_path: Path) -> Settings:
    return Settings(
        database_path=tmp_path / "data" / "app.db",
        upload_storage_path=tmp_path / "data" / "uploads",
        output_storage_path=tmp_path / "data" / "out",
        mcp_shared_dir=tmp_path / "shared",
        mcp_triage_poll_interval_seconds=0.01,
        mcp_triage_timeout_seconds=3,
        llm_provider="fake",
    )


@pytest.fixture
async def runtime(mcp_settings: Settings) -> AsyncIterator[McpRuntime]:
    configured = McpRuntime(mcp_settings)
    await configured.startup()
    try:
        yield configured
    finally:
        await configured.aclose()


@pytest.fixture
async def input_pdf(runtime: McpRuntime) -> Path:
    path = runtime.settings.mcp_shared_dir / "input" / "sample.pdf"

    def create_pdf() -> None:
        path.parent.mkdir(parents=True)
        with pymupdf.open() as document:
            page = document.new_page()
            page.insert_text((72, 72), "Hello World. Document Translation workflow.")
            document.save(path)

    await asyncio.to_thread(create_pdf)
    return path
