"""Translation execution and atomic chunk checkpoints."""

from __future__ import annotations

import asyncio
import json
import math
import time
from datetime import UTC, datetime
from uuid import uuid4

import structlog

from app.adapters.persistence.worker import WorkerPersistence
from app.config import Settings
from app.core.errors import ErrorCode, ProviderError
from app.core.models import (
    AttemptOutcome,
    Block,
    ChunkAttemptRecord,
    ChunkRecord,
    ChunkRequest,
    ChunkResult,
    JobRecord,
    TranslationPlan,
)
from app.core.ports import (
    CostCalculator,
    JobExecutionRepository,
    LLMProvider,
    TranslationCacheRepository,
)
from app.worker.executor import Executor
from app.worker.keys import translation_key

logger = structlog.get_logger(__name__)


class TranslationLoop:
    """Translate claimed chunks and checkpoint each terminal result atomically.

    Instances belong to one claimed job. The local cost reservation includes
    uncertain provider calls; SQLite records only usage the provider reported.
    """

    def __init__(
        self,
        settings: Settings,
        job_repo: JobExecutionRepository,
        cache_repo: TranslationCacheRepository,
        llm_provider: LLMProvider,
        cost_calculator: CostCalculator,
        *,
        persistence: WorkerPersistence,
    ) -> None:
        self._settings = settings
        self._job_repo = job_repo
        self._cache_repo = cache_repo
        self._llm_provider = llm_provider
        self._cost_calculator = cost_calculator
        self._persistence = persistence
        self.cost_lock = asyncio.Lock()
        self.cost_usd = 0.0
        self._cost_job_id: str | None = None

    async def process_chunk(
        self,
        job: JobRecord,
        chunk: ChunkRecord,
        blocks: list[Block],
        all_blocks: list[Block],
        plan: TranslationPlan,
    ) -> None:
        """Process one claimed chunk; exhausted provider failures are terminal."""
        ordered_blocks = sorted(blocks, key=lambda block: block.seq)
        if not ordered_blocks:
            await self._complete_chunk(job, chunk)
            return

        key = translation_key(job)
        async with self._persistence.read():
            cached: dict[str, str] = {}
            for block in ordered_blocks:
                translation = await self._cache_repo.get_block_translation(key, block.id)
                if translation is not None:
                    cached[block.id] = translation
            first_attempt_no = await self._persistence.next_attempt_no(chunk.id)

        missing = [block for block in ordered_blocks if block.id not in cached]
        if not missing:
            await self._complete_chunk(job, chunk)
            logger.info(
                "translation_chunk_cache_hit",
                job_id=job.id,
                chunk_id=chunk.id,
                block_count=len(ordered_blocks),
            )
            return

        policy = self._retry_policy(job)
        baselines = policy.get("attempt_baselines", {})
        baseline = baselines.get(chunk.id, 0) if isinstance(baselines, dict) else 0
        if isinstance(baseline, bool) or not isinstance(baseline, int) or baseline < 0:
            baseline = 0
        max_attempts = policy.get("max_chunk_attempts", self._settings.max_chunk_attempts)
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            max_attempts = self._settings.max_chunk_attempts
        attempts_since_retry = max(0, (first_attempt_no - 1) - baseline)
        remaining_attempts = max_attempts - attempts_since_retry
        if remaining_attempts <= 0:
            # A previous worker may have recorded its last retryable failure
            # and died before marking the chunk terminal.
            await self._complete_chunk(job, chunk)
            return

        context_before, context_after = self._source_neighbors(ordered_blocks, all_blocks)
        request = ChunkRequest(
            chunk_id=chunk.id,
            blocks=missing,
            target_language=job.target_language,
            plan=plan,
            glossary=job.glossary,
            model=job.model,
            context_before=context_before,
            context_after=context_after,
        )
        key = translation_key(job)
        calls_started = 0

        async def invoke_provider() -> tuple[ChunkResult, int]:
            nonlocal calls_started
            calls_started += 1
            return await self._invoke_provider(
                job,
                chunk,
                request,
                terminal_retry=calls_started >= remaining_attempts,
            )

        try:
            result, latency_ms = await Executor(remaining_attempts).execute(invoke_provider)
        except ProviderError:
            # Fatal and exhausted failures already atomically record their
            # diagnostic attempt and complete the chunk inside the callback.
            return

        await self._commit_success(job, chunk, key, missing, result, latency_ms)

    async def _invoke_provider(
        self,
        job: JobRecord,
        chunk: ChunkRecord,
        request: ChunkRequest,
        *,
        terminal_retry: bool,
    ) -> tuple[ChunkResult, int]:
        async with self._persistence.read():
            await self._persistence.ensure_owned(job.id, self._settings.worker_id, chunk.id)
        try:
            estimated_cost = self._estimated_cost(request)
            await self._reserve_cost(job, estimated_cost)
        except ProviderError as preflight_error:
            await self._record_failed_attempt(job, chunk, preflight_error, 0, terminal=True)
            if preflight_error.error_code is ErrorCode.COST_CAP_EXCEEDED:
                logger.warning(
                    "translation_cost_cap_reached",
                    job_id=job.id,
                    chunk_id=chunk.id,
                    error_code=preflight_error.error_code.value,
                )
            raise
        started = time.perf_counter()
        try:
            raw_result = await self._llm_provider.translate_chunk(request)
        except ProviderError as provider_error:
            latency_ms = self._elapsed_ms(started)
            known_cost = self._known_error_cost(job, provider_error)
            await self._settle_cost(estimated_cost, known_cost)
            terminal = not provider_error.retryable or terminal_retry
            await self._record_failed_attempt(
                job, chunk, provider_error, latency_ms, terminal=terminal
            )
            raise
        except Exception:
            latency_ms = self._elapsed_ms(started)
            # Provider adapters normally map failures to ProviderError. Keep
            # unexpected adapter exceptions safe and retryable at this boundary.
            unexpected_error = ProviderError(ErrorCode.PROVIDER_CONNECTION, model=job.model)
            await self._settle_cost(estimated_cost, None)
            await self._record_failed_attempt(
                job,
                chunk,
                unexpected_error,
                latency_ms,
                terminal=terminal_retry,
            )
            raise unexpected_error from None

        try:
            result = self._validate_result(raw_result, request)
            actual_cost = self._cost_calculator.estimate(
                result.model, result.tokens_in, result.tokens_out
            )
            latency_ms = self._elapsed_ms(started)
        except (TypeError, ValueError):
            latency_ms = self._elapsed_ms(started)
            usage = self._result_usage(raw_result)
            invalid_error = ProviderError(
                ErrorCode.PROVIDER_INVALID_RESPONSE,
                tokens_in=usage[0],
                tokens_out=usage[1],
                model=job.model,
            )
            known_cost = self._known_error_cost(job, invalid_error)
            await self._settle_cost(estimated_cost, known_cost)
            await self._record_failed_attempt(
                job,
                chunk,
                invalid_error,
                latency_ms,
                terminal=terminal_retry,
            )
            raise invalid_error from None

        await self._settle_cost(estimated_cost, actual_cost)
        return result, latency_ms

    @staticmethod
    def _validate_result(raw_result: object, request: ChunkRequest) -> ChunkResult:
        if not isinstance(raw_result, ChunkResult):
            raise TypeError("provider result has an unsupported type")
        if raw_result.model != request.model:
            raise ValueError("provider result model does not match the request")
        if (
            isinstance(raw_result.tokens_in, bool)
            or not isinstance(raw_result.tokens_in, int)
            or raw_result.tokens_in < 0
            or isinstance(raw_result.tokens_out, bool)
            or not isinstance(raw_result.tokens_out, int)
            or raw_result.tokens_out < 0
        ):
            raise ValueError("provider result usage is invalid")
        translations = raw_result.translations
        expected_ids = {block.id for block in request.blocks}
        if not isinstance(translations, dict) or set(translations) != expected_ids:
            raise ValueError("provider result block IDs do not match the request")
        if any(
            not isinstance(block_id, str) or not isinstance(text, str)
            for block_id, text in translations.items()
        ):
            raise ValueError("provider result translations must map strings to strings")
        return raw_result

    @staticmethod
    def _result_usage(result: object) -> tuple[int, int]:
        tokens_in = getattr(result, "tokens_in", 0)
        tokens_out = getattr(result, "tokens_out", 0)
        valid_in = (
            tokens_in
            if isinstance(tokens_in, int) and not isinstance(tokens_in, bool) and tokens_in > 0
            else 0
        )
        valid_out = (
            tokens_out
            if isinstance(tokens_out, int) and not isinstance(tokens_out, bool) and tokens_out > 0
            else 0
        )
        return valid_in, valid_out

    def _known_error_cost(self, job: JobRecord, error: ProviderError) -> float | None:
        if error.tokens_in <= 0 and error.tokens_out <= 0:
            return None
        try:
            return self._cost_calculator.estimate(job.model, error.tokens_in, error.tokens_out)
        except (TypeError, ValueError):
            return None

    def _estimated_cost(self, request: ChunkRequest) -> float:
        # UTF-8 bytes are a conservative token proxy for typical text. Output
        # receives a 3x expansion allowance for translation and a fixed prompt
        # overhead is included; this intentionally favors early cap rejection.
        input_texts = [
            request.target_language,
            request.plan.source_language,
            request.plan.domain,
            request.plan.register,
            *request.plan.terms,
            *request.plan.warnings,
            *[f"{source} → {target}" for source, target in request.glossary.items()],
            *[block.source_text for block in request.context_before],
            *[block.source_text for block in request.blocks],
            *[block.source_text for block in request.context_after],
        ]
        input_tokens = 256 + sum(len(value.encode("utf-8")) for value in input_texts)
        output_tokens = max(
            1,
            3 * sum(len(block.source_text.encode("utf-8")) for block in request.blocks),
        )
        try:
            return self._cost_calculator.estimate(request.model, input_tokens, output_tokens)
        except (TypeError, ValueError) as exc:
            raise ProviderError(ErrorCode.PROVIDER_BAD_REQUEST, model=request.model) from exc

    async def _reserve_cost(self, job: JobRecord, estimated_cost: float) -> None:
        async with self.cost_lock:
            self._initialize_cost(job)
            policy_cap = self._retry_policy(job).get("max_cost_per_job_usd")
            cap = self._settings.max_cost_per_job_usd
            if (
                isinstance(policy_cap, int | float)
                and not isinstance(policy_cap, bool)
                and math.isfinite(policy_cap)
                and policy_cap >= cap
            ):
                cap = float(policy_cap)
            if self.cost_usd + estimated_cost > cap:
                raise ProviderError(ErrorCode.COST_CAP_EXCEEDED, model=job.model)
            self.cost_usd += estimated_cost

    def _retry_policy(self, job: JobRecord) -> dict[str, object]:
        if not job.error_detail:
            return {}
        try:
            value = json.loads(job.error_detail)
        except (TypeError, ValueError):
            return {}
        if not isinstance(value, dict):
            return {}
        policy = value.get("_internal_retry_policy")
        return policy if isinstance(policy, dict) and policy.get("version") == 1 else {}

    async def _settle_cost(self, reserved: float, actual: float | None) -> None:
        if actual is None:
            return
        async with self.cost_lock:
            self.cost_usd += actual - reserved

    def _initialize_cost(self, job: JobRecord) -> None:
        if self._cost_job_id != job.id:
            self._cost_job_id = job.id
            self.cost_usd = job.cost_usd

    async def _record_failed_attempt(
        self,
        job: JobRecord,
        chunk: ChunkRecord,
        error: ProviderError,
        latency_ms: int,
        *,
        terminal: bool,
    ) -> None:
        outcome = AttemptOutcome.RETRYABLE_ERROR if error.retryable else AttemptOutcome.FATAL_ERROR
        cost = self._known_error_cost(job, error) or 0.0
        attempt = ChunkAttemptRecord(
            id=str(uuid4()),
            chunk_id=chunk.id,
            attempt_no=0,
            tokens_in=max(0, error.tokens_in),
            tokens_out=max(0, error.tokens_out),
            cost_usd=cost,
            latency_ms=latency_ms,
            outcome=outcome,
            error_detail=error.message,
            created_at=datetime.now(UTC),
        )
        async with self._persistence.write():
            await self._persistence.ensure_owned(job.id, self._settings.worker_id, chunk.id)
            attempt_no = await self._persistence.next_attempt_no(chunk.id)
            await self._job_repo.record_chunk_attempt(
                attempt.model_copy(update={"attempt_no": attempt_no})
            )
            if terminal:
                await self._complete_and_progress(job, chunk)
        logger.warning(
            "translation_attempt_failed",
            job_id=job.id,
            chunk_id=chunk.id,
            attempt_no=attempt_no,
            outcome=outcome.value,
            error_code=error.error_code.value,
            terminal=terminal,
        )

    async def _commit_success(
        self,
        job: JobRecord,
        chunk: ChunkRecord,
        key: str,
        missing: list[Block],
        result: ChunkResult,
        latency_ms: int,
    ) -> None:
        attempt = ChunkAttemptRecord(
            id=str(uuid4()),
            chunk_id=chunk.id,
            attempt_no=0,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            cost_usd=self._cost_calculator.estimate(
                result.model, result.tokens_in, result.tokens_out
            ),
            latency_ms=latency_ms,
            outcome=AttemptOutcome.OK,
            error_detail=None,
            created_at=datetime.now(UTC),
        )
        async with self._persistence.write():
            await self._persistence.ensure_owned(job.id, self._settings.worker_id, chunk.id)
            for block in missing:
                await self._cache_repo.save_block_translation(
                    key, block.id, result.translations[block.id]
                )
            attempt_no = await self._persistence.next_attempt_no(chunk.id)
            await self._job_repo.record_chunk_attempt(
                attempt.model_copy(update={"attempt_no": attempt_no})
            )
            await self._complete_and_progress(job, chunk)

    async def _complete_chunk(self, job: JobRecord, chunk: ChunkRecord) -> None:
        async with self._persistence.write():
            await self._persistence.ensure_owned(job.id, self._settings.worker_id, chunk.id)
            await self._complete_and_progress(job, chunk)

    async def _complete_and_progress(self, job: JobRecord, chunk: ChunkRecord) -> None:
        await self._job_repo.complete_chunk(chunk.id)
        fresh_job = await self._job_repo.get_job(job.id)
        if fresh_job is None:
            raise RuntimeError("job disappeared while completing a chunk")
        await self._job_repo.update_job_progress(job.id, fresh_job.done_chunks + 1)
        persisted_job = await self._job_repo.get_job(job.id)
        if persisted_job is None:
            raise RuntimeError("job disappeared after updating progress")
        logger.info(
            "translation_chunk_checkpointed",
            job_id=job.id,
            chunk_id=chunk.id,
            done_chunks=persisted_job.done_chunks,
        )

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return max(0, round((time.perf_counter() - started) * 1000))

    @staticmethod
    def _source_neighbors(
        chunk_blocks: list[Block], all_blocks: list[Block]
    ) -> tuple[list[Block], list[Block]]:
        ordered_document = sorted(all_blocks, key=lambda block: block.seq)
        chunk_ids = {block.id for block in chunk_blocks}
        first_seq = min(block.seq for block in chunk_blocks)
        last_seq = max(block.seq for block in chunk_blocks)
        before = [
            block
            for block in ordered_document
            if block.seq < first_seq and block.id not in chunk_ids
        ]
        after = [
            block
            for block in ordered_document
            if block.seq > last_seq and block.id not in chunk_ids
        ]
        return (before[-1:], after[:1])
