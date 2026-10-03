"""Analyze extracted documents outside transactions and publish atomic results."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

import structlog

from app.core.errors import ErrorCode, ProviderError
from app.core.models import (
    Block,
    DocumentAnalysisRecord,
    DocumentIR,
    DocumentRecord,
    DocumentStatus,
    TranslationPlan,
    TriageResult,
    TriageStatus,
)
from app.core.ports import CostCalculator, TriageAgent
from app.core.services.document_service import TransactionContext

logger = structlog.get_logger(__name__)


class AnalysisRepository(Protocol):
    """Internal subset of document persistence used by triage."""

    async def get_document(self, document_id: str) -> DocumentRecord | None: ...
    async def get_blocks(self, document_id: str) -> list[Block]: ...
    async def get_analysis(self, document_id: str) -> DocumentAnalysisRecord | None: ...
    async def save_analysis(
        self,
        document_id: str,
        plan: TranslationPlan,
        *,
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost_usd: float = 0.0,
        cost_usd_total: float | None = None,
        tokens_in_total: int | None = None,
        tokens_out_total: int | None = None,
    ) -> DocumentAnalysisRecord: ...
    async def update_document_status(
        self, document_id: str, status: DocumentStatus, error_code: str | None = None
    ) -> None: ...


@dataclass(frozen=True)
class _AttemptUsage:
    model: str | None
    tokens_in: int
    tokens_out: int
    cached_tokens_in: int
    requests: int


def _count(value: object) -> int:
    """Accept only safe non-negative integer usage from injected agents/errors."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _usage_from_result(value: object) -> _AttemptUsage:
    if isinstance(value, dict):
        model = value.get("model")
        tokens_in = value.get("tokens_in", 0)
        tokens_out = value.get("tokens_out", 0)
        cached_tokens_in = value.get("cached_tokens_in", 0)
        requests = value.get("requests", 0)
    else:
        model = getattr(value, "model", None)
        tokens_in = getattr(value, "tokens_in", 0)
        tokens_out = getattr(value, "tokens_out", 0)
        cached_tokens_in = getattr(value, "cached_tokens_in", 0)
        requests = getattr(value, "requests", 0)
    return _AttemptUsage(
        model=model if isinstance(model, str) else None,
        tokens_in=_count(tokens_in),
        tokens_out=_count(tokens_out),
        cached_tokens_in=_count(cached_tokens_in),
        requests=_count(requests),
    )


def _usage_from_error(error: Exception) -> _AttemptUsage:
    current: BaseException | None = error
    while current is not None:
        if isinstance(current, ProviderError) or hasattr(current, "tokens_in"):
            model = getattr(current, "model", None)
            return _AttemptUsage(
                model=model if isinstance(model, str) else None,
                tokens_in=_count(getattr(current, "tokens_in", 0)),
                tokens_out=_count(getattr(current, "tokens_out", 0)),
                cached_tokens_in=_count(getattr(current, "cached_tokens_in", 0)),
                requests=_count(getattr(current, "requests", 0)),
            )
        current = current.__cause__
    return _AttemptUsage(None, 0, 0, 0, 0)


class TriageService:
    def __init__(
        self,
        repository: AnalysisRepository,
        agent: TriageAgent,
        cost_calculator: CostCalculator,
        transaction_context: TransactionContext,
        discard_degraded_analysis: Callable[[str], Awaitable[None]],
        *,
        claim_analysis: Callable[[str], Awaitable[bool]] | None = None,
        analysis_in_use: Callable[[str], Awaitable[bool]] | None = None,
        # Keep the service guard outside OpenAITriageAgent's 60s timeout so
        # the adapter can raise ProviderError with its accumulated usage first.
        attempt_timeout_seconds: float = 65,
        retry_delay_seconds: float = 0.1,
    ) -> None:
        self._repository = repository
        self._agent = agent
        self._cost_calculator = cost_calculator
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
        provider_plan_succeeded = False
        plan_usage = _AttemptUsage(None, 0, 0, 0, 0)
        attempts: list[_AttemptUsage] = []
        for attempt in range(3):
            candidate: object | None = None
            provider_returned = False
            try:
                async with asyncio.timeout(self._attempt_timeout_seconds):
                    candidate = await self._agent.analyze(document_ir)
                    provider_returned = True
                # Preserve usage even when an injected implementation returns
                # an invalid DTO; the invocation may still have been billed.
                usage = _usage_from_result(candidate)
                attempts.append(usage)
                validated = TriageResult.model_validate(candidate)
                plan = TranslationPlan.model_validate(validated.plan)
                plan_usage = _usage_from_result(validated)
                provider_plan_succeeded = True
                break
            except Exception as error:
                if not provider_returned:
                    attempts.append(_usage_from_error(error))
                terminal = isinstance(error, ProviderError) and (
                    not error.retryable or error.terminal
                )
                logger.warning(
                    "triage_attempt_failed",
                    document_id=document_id,
                    attempt=attempt + 1,
                    terminal=terminal,
                )
                if terminal:
                    break
                if attempt < 2:
                    await asyncio.sleep(self._retry_delay_seconds * (2**attempt))
        if plan is None:
            plan = degraded_plan(document_ir)
        attempt_costs = [self._cost(usage) for usage in attempts]
        plan_cost = self._cost(plan_usage) if provider_plan_succeeded else 0.0
        cost_delta = sum(attempt_costs)
        tokens_in_delta = sum(usage.tokens_in for usage in attempts)
        tokens_out_delta = sum(usage.tokens_out for usage in attempts)
        recorded_attempts: list[tuple[_AttemptUsage, float]] = []
        async with self._transaction():
            # A competing successful task must never be overwritten by fallback.
            current = await self._repository.get_analysis(document_id)
            frozen = self._analysis_in_use is not None and await self._analysis_in_use(document_id)
            if not frozen and (current is None or current.triage_status is TriageStatus.DEGRADED):
                # The degraded row is removed before republishing. Carry its
                # complete cumulative totals across that delete in this same
                # transaction so a re-triage cannot reset the durable series.
                previous_cost = 0.0 if current is None else current.cost_usd_total
                previous_input = 0 if current is None else current.tokens_in_total
                previous_output = 0 if current is None else current.tokens_out_total
                await self._discard_degraded_analysis(document_id)
                await self._repository.save_analysis(
                    document_id,
                    plan,
                    tokens_in=plan_usage.tokens_in if provider_plan_succeeded else 0,
                    tokens_out=plan_usage.tokens_out if provider_plan_succeeded else 0,
                    cost_usd=plan_cost,
                    cost_usd_total=previous_cost + cost_delta,
                    tokens_in_total=previous_input + tokens_in_delta,
                    tokens_out_total=previous_output + tokens_out_delta,
                )
                recorded_attempts = list(zip(attempts, attempt_costs, strict=True))
            await self._repository.update_document_status(document_id, DocumentStatus.EXTRACTED)
        for usage, cost in recorded_attempts:
            logger.info(
                "triage_cost_recorded",
                document_id=document_id,
                tokens_in=usage.tokens_in,
                tokens_out=usage.tokens_out,
                cached_tokens_in=usage.cached_tokens_in,
                cost_usd=cost,
                model=usage.model,
                requests=usage.requests,
                tokens_in_delta=usage.tokens_in,
                tokens_out_delta=usage.tokens_out,
                cost_usd_delta=cost,
            )
        logger.info("triage_completed", document_id=document_id, triage_status=plan.triage_status)

    def _cost(self, usage: _AttemptUsage) -> float:
        if usage.model is None:
            return 0.0
        try:
            return self._cost_calculator.estimate_usage(
                usage.model,
                usage.tokens_in,
                usage.tokens_out,
                usage.cached_tokens_in,
            )
        except ValueError:
            # A newly configured provider model without a price must not leave
            # documents stuck in ANALYZING. Tokens remain durable and the safe
            # model label makes the unpriced delta visible in logs.
            logger.error("triage_cost_estimation_failed", model=usage.model)
            return 0.0


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
