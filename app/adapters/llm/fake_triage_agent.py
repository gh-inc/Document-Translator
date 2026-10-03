"""Deterministic text-only triage with configurable fake provider faults."""

from __future__ import annotations

import asyncio
import random
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings
from app.core.errors import ErrorCode, ProviderError
from app.core.models import DocumentIR, TranslationPlan, TriageResult

_FAILURE_CODES = {
    "429": ErrorCode.PROVIDER_RATE_LIMIT,
    "500": ErrorCode.PROVIDER_SERVER_ERROR,
    "timeout": ErrorCode.PROVIDER_TIMEOUT,
}
MAX_TERMS = 20
MAX_SAMPLE_CHARS = 20_000
_FAKE_USAGE = {"tokens_in": 173, "tokens_out": 29, "cached_tokens_in": 61, "requests": 3}


class _FakeTriageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fail_rate: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    fail_mode: Literal["429", "500", "timeout"]
    latency_ms: int = Field(ge=0)


def heuristic_plan(document: DocumentIR) -> TranslationPlan:
    """Limited English/Ukrainian/Russian heuristic for deterministic local triage."""
    sample_parts: list[str] = []
    remaining = MAX_SAMPLE_CHARS
    for block in sorted(document.blocks, key=lambda block: block.seq):
        sample_parts.append(block.source_text[:remaining])
        remaining -= len(sample_parts[-1])
        if remaining <= 0:
            break
    sample = "\n".join(sample_parts)
    if re.search(r"[\u0400-\u04ff]", sample):
        language = "uk" if re.search(r"[іїєґІЇЄҐ]", sample) else "ru"
    else:
        language = "en"
    terms: list[str] = []
    for word in re.findall(r"\b[^\W\d_]+\b", sample):
        if word[0].isupper() and word not in terms:
            terms.append(word)
        if len(terms) >= MAX_TERMS:
            break
    return TranslationPlan(
        source_language=language, domain="general", register="neutral", terms=terms
    )


class FakeTriageAgent:
    """Produce a repeatable plan, with async latency and injected catalogued failures."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        fail_rate: float | None = None,
        fail_mode: str | None = None,
        latency_ms: int | None = None,
        rng: random.Random | None = None,
    ) -> None:
        configured = settings or Settings()
        self.config = _FakeTriageConfig.model_validate(
            {
                "fail_rate": configured.fake_fail_rate if fail_rate is None else fail_rate,
                "fail_mode": configured.fake_fail_mode if fail_mode is None else fail_mode,
                "latency_ms": configured.fake_latency_ms if latency_ms is None else latency_ms,
            }
        )
        self._rng = rng or random.Random()
        self._model = configured.triage_model.strip() or "gpt-4o"

    async def analyze(self, document: DocumentIR) -> TriageResult:
        if self.config.latency_ms:
            await asyncio.sleep(self.config.latency_ms / 1000)
        if self._rng.random() < self.config.fail_rate:
            raise ProviderError(_FAILURE_CODES[self.config.fail_mode], model=self._model)
        plan = await asyncio.to_thread(heuristic_plan, document)
        return TriageResult(plan=plan, model=self._model, **_FAKE_USAGE)

    async def aclose(self) -> None:
        """No resources are held by the fake adapter."""
