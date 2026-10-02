"""Shared REST/MCP triage ownership with crash-released advisory file locks."""

from collections.abc import Callable
from contextlib import suppress

import structlog

from app.adapters.llm.fake_triage_agent import FakeTriageAgent
from app.adapters.llm.triage_agent import OpenAITriageAgent
from app.adapters.persistence.database import _finish_cleanup
from app.adapters.persistence.triage import ScopedTriageRepository
from app.adapters.storage.document_locks import release_document_lock, try_document_lock
from app.config import Settings
from app.core.models import DocumentIR, TranslationPlan
from app.core.ports import TriageAgent
from app.core.services.triage_service import TriageService

logger = structlog.get_logger(__name__)
AgentFactory = Callable[[Settings], TriageAgent]


def create_triage_agent(settings: Settings) -> TriageAgent:
    if settings.llm_provider == "fake":
        return FakeTriageAgent(settings=settings)
    return OpenAITriageAgent(settings=settings)


class _LazyAgent:
    def __init__(self, settings: Settings, factory: AgentFactory) -> None:
        self._settings = settings
        self._factory = factory
        self._agent: TriageAgent | None = None

    async def analyze(self, document: DocumentIR) -> TranslationPlan:
        if self._agent is None:
            self._agent = self._factory(self._settings)
        return await self._agent.analyze(document)

    async def aclose(self) -> None:
        if self._agent is not None:
            close = getattr(self._agent, "aclose", None)
            if close is not None:
                with suppress(Exception):
                    await close()


class ClaimedTriage:
    """Exclusive claim transferred to one background task by its caller."""

    def __init__(
        self, document_id: str, descriptor: int, service: TriageService, agent: _LazyAgent
    ) -> None:
        self.document_id = document_id
        self._descriptor: int | None = descriptor
        self._service = service
        self._agent = agent
        self._started = False

    async def run(self) -> None:
        if self._descriptor is None or self._started:
            return
        self._started = True
        try:
            await self._service.run(self.document_id)
        except Exception:
            logger.error("triage_background_failed", document_id=self.document_id)
        finally:
            try:
                await _finish_cleanup(self._agent.aclose())
            finally:
                await self.close()

    async def close(self) -> None:
        descriptor, self._descriptor = self._descriptor, None
        if descriptor is not None:
            await release_document_lock(descriptor)


async def prepare_triage(
    document_id: str,
    settings: Settings,
    agent_factory: AgentFactory = create_triage_agent,
) -> ClaimedTriage | None:
    """Acquire ownership and commit a claim before scheduling provider work.

    The filesystem lock distinguishes live ANALYZING from a crashed owner
    without a schema lease. SQLite gates eligible status, successful analysis,
    and analysis frozen by an existing translation job.
    """
    descriptor = await try_document_lock(settings.database_path, document_id, "triage")
    if descriptor is None:
        return None
    repository = ScopedTriageRepository(settings.database_path)
    agent = _LazyAgent(settings, agent_factory)
    service = TriageService(
        repository,
        agent,
        repository.transaction,
        repository.discard_degraded_analysis,
        claim_analysis=repository.claim_analysis,
        analysis_in_use=repository.has_jobs,
    )
    claimed = ClaimedTriage(document_id, descriptor, service, agent)
    try:
        if await service.claim(document_id):
            return claimed
    except BaseException:
        await claimed.close()
        raise
    await claimed.close()
    return None
