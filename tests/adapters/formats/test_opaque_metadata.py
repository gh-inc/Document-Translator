"""Format metadata remains opaque across the core/provider boundary."""

from copy import deepcopy

import pytest

from app.adapters.llm.fake_provider import FakeProvider
from app.core.models import Block, ChunkRequest, TranslationPlan


@pytest.mark.parametrize(
    "metadata",
    [
        {"page": "not a page", "bbox": None},
        {"paragraph_index": [False], "style": 42},
        {"unknown": {"nested": [None, True, "unchanged"]}},
    ],
)
async def test_malformed_opaque_metadata_does_not_affect_translation(metadata: dict) -> None:
    original = deepcopy(metadata)
    request = ChunkRequest(
        chunk_id="opaque-chunk",
        blocks=[
            Block(
                id="opaque-block",
                seq=0,
                source_text="Business document",
                source_hash="unused-by-provider",
                format_metadata=metadata,
            )
        ],
        target_language="de",
        plan=TranslationPlan(source_language="en", domain="business", register="neutral"),
        model="gpt-4o-mini",
    )
    restored = ChunkRequest.model_validate_json(request.model_dump_json())
    result = await FakeProvider(fail_rate=0, latency_ms=0).translate_chunk(restored)
    assert result.translations == {"opaque-block": "[de] Business document"}
    assert restored.blocks[0].format_metadata == original
    assert request.blocks[0].format_metadata == original

    plain = restored.model_copy(
        update={"blocks": [restored.blocks[0].model_copy(update={"format_metadata": {}})]}
    )
    baseline = await FakeProvider(fail_rate=0, latency_ms=0).translate_chunk(plain)
    assert baseline == result
