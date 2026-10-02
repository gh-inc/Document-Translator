"""Analyze extracted documents outside transactions and publish atomic results."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol

import structlog

from app.core.errors import ErrorCode
from app.core.models import (
    Block,
    DocumentAnalysisRecord,
    DocumentIR,
    DocumentRecord,
    DocumentStatus,
    TranslationPlan,
    TriageStatus,
)
from app.core.ports import TriageAgent
from app.core.services.document_service import TransactionContext

logger = structlog.get_logger(__name__)


class AnalysisRepository(Protocol):
    """Internal subset of document persistence used by triage."""

    async def get_document(self, document_id: str) -> DocumentRecord | None: ...
    async def get_blocks(self, document_id: str) -> list[Block]: ...
    async def get_analysis(self, document_id: str) -> DocumentAnalysisRecord | None: ...
    async def save_analysis(
        self, document_id: str, plan: TranslationPlan
    ) -> DocumentAnalysisRecord: ...
    async def update_document_status(
        self, document_id: str, status: DocumentStatus, error_code: str | None = None
    ) -> None: ...


class TriageService:
    def __init__(
        self,
        repository: AnalysisRepository,
        agent: TriageAgent,
        transaction_context: TransactionContext,
        discard_degraded_analysis: Callable[[str], Awaitable[None]],
        *,
        claim_analysis: Callable[[str], Awaitable[bool]] | None = None,
        analysis_in_use: Callable[[str], Awaitable[bool]] | None = None,
        attempt_timeout_seconds: float = 60,
        retry_delay_seconds: float = 0.1,
    ) -> None:
        self._repository = repository
        self._agent = agent
        self._transaction = transaction_context
        self._discard_degraded_analysis = discard_degraded_analysis
        self._claim_analysis = claim_analysis
        self._analysis_in_use = analysis_in_use
        self._attempt_timeout_seconds = attempt_timeout_seconds
        self._retry_delay_seconds = retry_delay_seconds

    async def claim(self, document_id: str) -> bool:
        """Commit one conditional claim before scheduling any background work."""
        if self._claim_analysis is None:
            raise RuntimeError("triage claim persistence is not configured")
        async with self._transaction():
            if await self._claim_analysis(document_id):
                return True
            # Old interrupted transitions can leave a persisted immutable
            # analysis marked ANALYZING. Repair readiness without invoking or
            # overwriting the agent contract.
            document = await self._repository.get_document(document_id)
            analysis = await self._repository.get_analysis(document_id)
            frozen = self._analysis_in_use is not None and await self._analysis_in_use(document_id)
            if (
                document is not None
                and document.status is DocumentStatus.ANALYZING
                and analysis is not None
                and (analysis.triage_status is TriageStatus.OK or frozen)
            ):
                await self._repository.update_document_status(document_id, DocumentStatus.EXTRACTED)
            return False

    async def run(self, document_id: str) -> None:
        document = await self._repository.get_document(document_id)
        if document is None or document.status is not DocumentStatus.ANALYZING:
            return
        analysis = await self._repository.get_analysis(document_id)
        if analysis is not None and analysis.triage_status is TriageStatus.OK:
            async with self._transaction():
                await self._repository.update_document_status(document_id, DocumentStatus.EXTRACTED)
            return
        blocks = await self._repository.get_blocks(document_id)
        if not blocks:
            async with self._transaction():
                await self._repository.update_document_status(
                    document_id, DocumentStatus.FAILED, ErrorCode.CORRUPT_FILE
                )
            return
        document_ir = DocumentIR(
            id=document.id,
            filename=document.filename,
            format=document.format,
            size_bytes=document.size_bytes,
            page_count=document.page_count,
            blocks=blocks,
        )
        plan: TranslationPlan | None = None
        for attempt in range(3):
            try:
                async with asyncio.timeout(self._attempt_timeout_seconds):
                    candidate = await self._agent.analyze(document_ir)
                # Validate even implementations injected by application tests.
                plan = TranslationPlan.model_validate(candidate)
                break
            except Exception:
                logger.warning(
                    "triage_attempt_failed", document_id=document_id, attempt=attempt + 1
                )
                if attempt < 2:
                    await asyncio.sleep(self._retry_delay_seconds * (2**attempt))
        if plan is None:
            plan = degraded_plan(document_ir)
        async with self._transaction():
            # A competing successful task must never be overwritten by fallback.
            current = await self._repository.get_analysis(document_id)
            frozen = self._analysis_in_use is not None and await self._analysis_in_use(document_id)
            if not frozen and (current is None or current.triage_status is TriageStatus.DEGRADED):
                await self._discard_degraded_analysis(document_id)
                await self._repository.save_analysis(document_id, plan)
            await self._repository.update_document_status(document_id, DocumentStatus.EXTRACTED)
        logger.info("triage_completed", document_id=document_id, triage_status=plan.triage_status)


def degraded_plan(document: DocumentIR) -> TranslationPlan:
    """Conservative bounded heuristic with an explicit uncertainty warning."""
    sample = "\n".join(block.source_text[:1000] for block in document.blocks[:20])
    cyrillic = sum("\u0400" <= char <= "\u04ff" for char in sample)
    latin = sum("a" <= char.casefold() <= "z" for char in sample)
    language = "en"
    if cyrillic > latin:
        language = "uk" if any(char in sample.casefold() for char in "іїєґ") else "ru"
    return TranslationPlan(
        source_language=language,
        domain="general",
        register="neutral",
        terms=[],
        warnings=["Automatic analysis failed; language and style use a limited heuristic."],
        triage_status=TriageStatus.DEGRADED,
    )
