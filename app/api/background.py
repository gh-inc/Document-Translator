"""Background composition; each analysis owns and closes its own connection."""

import asyncio
from collections.abc import Callable
from contextlib import suppress

import structlog

from app.adapters.llm.fake_triage_agent import FakeTriageAgent
from app.adapters.llm.triage_agent import OpenAITriageAgent
from app.adapters.persistence.database import SqliteConnectionFactory, transaction
from app.adapters.persistence.repositories import SqliteDocumentRepository
from app.adapters.persistence.triage import TriagePersistence
from app.config import Settings
from app.core.ports import TriageAgent
from app.core.services.triage_service import TriageService

logger = structlog.get_logger(__name__)
AgentFactory = Callable[[Settings], TriageAgent]


def create_triage_agent(settings: Settings) -> TriageAgent:
    if settings.llm_provider == "fake":
        return FakeTriageAgent(settings=settings)
    return OpenAITriageAgent(settings=settings)


async def run_triage(
    document_id: str,
    settings: Settings,
    lock: asyncio.Lock,
    agent_factory: AgentFactory = create_triage_agent,
) -> None:
    """Serialize analyses for this document within the single web process."""
    async with lock:
        connection = None
        agent = None
        try:
            connection = await SqliteConnectionFactory(settings.database_path).create()
            agent = agent_factory(settings)
            service = TriageService(
                SqliteDocumentRepository(connection),
                agent,
                lambda: transaction(connection),
                TriagePersistence(connection).discard_degraded_analysis,
            )
            await service.run(document_id)
        except Exception:
            # The response has already been sent. Persisted ANALYZING can be
            # recovered through explicit retry after storage/DB failures.
            logger.error("triage_background_failed", document_id=document_id)
        finally:
            if agent is not None:
                close = getattr(agent, "aclose", None)
                if close is not None:
                    with suppress(Exception):
                        await close()
            if connection is not None:
                await connection.close()
