"""Deterministic async LLM provider for local development and tests."""

from __future__ import annotations

import asyncio
import random
from functools import cache
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from tiktoken import Encoding

from app.config import Settings
from app.core.errors import ErrorCode, ProviderError
from app.core.models import ChunkRequest, ChunkResult

_FAILURE_CODES = {
    "429": ErrorCode.PROVIDER_RATE_LIMIT,
    "500": ErrorCode.PROVIDER_SERVER_ERROR,
    "timeout": ErrorCode.PROVIDER_TIMEOUT,
}


class _FakeProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fail_rate: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    fail_mode: Literal["429", "500", "timeout"]
    latency_ms: int = Field(ge=0)


@cache
def _get_encoder() -> Encoding:
    """Load and retain the fake provider's shared model encoder on first use."""
    import tiktoken

    return tiktoken.encoding_for_model("gpt-4o-mini")


def _count_usage_sync(input_text: str, output_text: str) -> tuple[int, int]:
    encoder = _get_encoder()
    return (
        len(encoder.encode(input_text, disallowed_special=())),
        len(encoder.encode(output_text, disallowed_special=())),
    )


class FakeProvider:
    """Prefix source text with the target language and simulate provider faults."""

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
        self.config = _FakeProviderConfig.model_validate(
            {
                "fail_rate": configured.fake_fail_rate if fail_rate is None else fail_rate,
                "fail_mode": configured.fake_fail_mode if fail_mode is None else fail_mode,
                "latency_ms": configured.fake_latency_ms if latency_ms is None else latency_ms,
            }
        )
        self._rng = rng or random.Random()

    async def translate_chunk(self, request: ChunkRequest) -> ChunkResult:
        if self.config.latency_ms:
            await asyncio.sleep(self.config.latency_ms / 1000)

        if self._rng.random() < self.config.fail_rate:
            raise ProviderError(_FAILURE_CODES[self.config.fail_mode], model=request.model)

        translations = {
            block.id: f"[{request.target_language}] {block.source_text}" for block in request.blocks
        }
        input_text = "\n".join(
            [
                request.target_language,
                request.plan.source_language,
                request.plan.domain,
                request.plan.register,
                *request.plan.terms,
                *request.plan.warnings,
                *[f"{source} → {target}" for source, target in sorted(request.glossary.items())],
                *[block.source_text for block in request.context_before],
                *[block.source_text for block in request.blocks],
                *[block.source_text for block in request.context_after],
            ]
        )
        output_text = "\n".join(translations.values())
        tokens_in, tokens_out = await asyncio.to_thread(_count_usage_sync, input_text, output_text)
        return ChunkResult(
            translations=translations,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model=request.model,
        )
