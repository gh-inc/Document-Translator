"""Compatibility entrypoint over shared REST/MCP triage composition."""

import asyncio

from app.adapters.llm.triage_runtime import (
    AgentFactory,
    prepare_triage,
)
from app.adapters.llm.triage_runtime import (
    create_triage_agent as create_triage_agent,
)
from app.config import Settings


async def run_triage(
    document_id: str,
    settings: Settings,
    lock: asyncio.Lock | None = None,
    agent_factory: AgentFactory = create_triage_agent,
) -> None:
    """Claim and run; legacy callers may still pass their local lock."""
    claimed = await prepare_triage(document_id, settings, agent_factory)
    if claimed is not None:
        await claimed.run()
