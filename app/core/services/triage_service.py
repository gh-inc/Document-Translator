"""Analyze extracted documents outside transactions and publish atomic results."""

import asyncio
from collections.abc import Awaitable, Callable

import structlog

from app.core.errors import ErrorCode
from app.core.models import DocumentIR, DocumentStatus, TranslationPlan, TriageStatus
from app.core.ports import DocumentRepository, TriageAgent
from app.core.services.document_service import TransactionContext

logger = structlog.get_logger(__name__)


class TriageService:
    def __init__(
        self,
        repository: DocumentRepository,
        agent: TriageAgent,
        transaction_context: TransactionContext,
        discard_degraded_analysis: Callable[[str], Awaitable[None]],
        *,
        attempt_timeout_seconds: float = 60,
        retry_delay_seconds: float = 0.1,
    ) -> None:
        self._repository = repository
        self._agent = agent
        self._transaction = transaction_context
        self._discard_degraded_analysis = discard_degraded_analysis
        self._attempt_timeout_seconds = attempt_timeout_seconds
        self._retry_delay_seconds = retry_delay_seconds

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
            if current is None or current.triage_status is TriageStatus.DEGRADED:
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
