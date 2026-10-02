"""Reusable behavioral smoke contract for real and fake LLM adapters."""

from app.core.models import Block, ChunkRequest, TranslationPlan
from app.core.ports import LLMProvider


def smoke_request(model: str = "gpt-4o-mini") -> ChunkRequest:
    return ChunkRequest(
        chunk_id="contract-chunk",
        blocks=[
            Block(id="block-1", seq=1, source_text="Hello", source_hash="hash-1"),
            Block(id="block-2", seq=2, source_text="Good morning", source_hash="hash-2"),
        ],
        context_before=[
            Block(id="before", seq=0, source_text="Introduction", source_hash="hash-0")
        ],
        context_after=[Block(id="after", seq=3, source_text="Conclusion", source_hash="hash-3")],
        target_language="de",
        plan=TranslationPlan(source_language="en", domain="general", register="neutral"),
        model=model,
    )


async def run_provider_smoke(provider: LLMProvider, model: str = "gpt-4o-mini") -> None:
    result = await provider.translate_chunk(smoke_request(model))
    assert set(result.translations) == {"block-1", "block-2"}
    assert all(result.translations.values())
    assert result.tokens_in > 0
    assert result.tokens_out > 0
    assert result.model
