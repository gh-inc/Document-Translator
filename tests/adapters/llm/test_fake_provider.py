from __future__ import annotations

import asyncio
import random
import threading

import pytest
import tiktoken
from pydantic import ValidationError
from tiktoken import Encoding

from app.adapters.llm import fake_provider
from app.adapters.llm.fake_provider import FakeProvider
from app.config import Settings
from app.core.errors import ErrorCode, ProviderError
from app.core.models import Block, ChunkRequest, TranslationPlan

_ORIGINAL_GET_ENCODER = fake_provider._get_encoder
_CACHE_TEST_ENCODING = Encoding(
    name="fake-provider-cache-test-encoding",
    pat_str=r"(?s).",
    mergeable_ranks={bytes([value]): value for value in range(256)},
    special_tokens={},
)


def make_request() -> ChunkRequest:
    return ChunkRequest(
        chunk_id="chunk-1",
        blocks=[
            Block(
                id="block-1",
                seq=1,
                source_text="Hello",
                source_hash="hash-1",
                format_metadata={"uninterpreted": [None, {"unknown": True}]},
            ),
            Block(
                id="block-2",
                seq=2,
                source_text="How are you?",
                source_hash="hash-2",
            ),
        ],
        target_language="de",
        plan=TranslationPlan(source_language="en", domain="general", register="neutral"),
        glossary={},
        model="gpt-4o-mini",
    )


async def test_fake_provider_translates_blocks_and_reports_usage() -> None:
    result = await FakeProvider().translate_chunk(make_request())

    assert result.translations == {
        "block-1": "[de] Hello",
        "block-2": "[de] How are you?",
    }
    assert result.tokens_in > 0
    assert result.tokens_out > 0
    assert result.model == "gpt-4o-mini"


@pytest.mark.parametrize(
    ("mode", "error_code"),
    [
        ("429", ErrorCode.PROVIDER_RATE_LIMIT),
        ("500", ErrorCode.PROVIDER_SERVER_ERROR),
        ("timeout", ErrorCode.PROVIDER_TIMEOUT),
    ],
)
async def test_fake_provider_failure_modes_use_catalogued_errors(
    mode: str, error_code: ErrorCode
) -> None:
    provider = FakeProvider(fail_rate=1.0, fail_mode=mode)

    with pytest.raises(ProviderError) as error:
        await provider.translate_chunk(make_request())

    assert error.value.error_code == error_code
    assert error.value.retryable is True
    assert error.value.model == "gpt-4o-mini"


async def test_explicit_zero_overrides_settings_and_metadata_is_opaque() -> None:
    provider = FakeProvider(
        settings=Settings(fake_fail_rate=1.0, fake_fail_mode="500", fake_latency_ms=500),
        fail_rate=0.0,
        fail_mode="timeout",
        latency_ms=0,
    )

    result = await provider.translate_chunk(make_request())

    assert result.translations["block-1"] == "[de] Hello"
    assert provider.config.fail_mode == "timeout"
    assert provider.config.latency_ms == 0


async def test_fake_provider_latency_is_async_and_cancellation_propagates() -> None:
    provider = FakeProvider(latency_ms=500)
    task = asyncio.create_task(provider.translate_chunk(make_request()))
    await asyncio.sleep(0.01)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


async def test_tokenizer_work_runs_outside_the_event_loop(monkeypatch) -> None:
    event_loop_thread = threading.get_ident()
    tokenizer_threads: list[int] = []
    count_usage = fake_provider._count_usage_sync

    def record_thread(input_text: str, output_text: str) -> tuple[int, int]:
        tokenizer_threads.append(threading.get_ident())
        return count_usage(input_text, output_text)

    monkeypatch.setattr(fake_provider, "_count_usage_sync", record_thread)
    await FakeProvider().translate_chunk(make_request())

    assert tokenizer_threads
    assert tokenizer_threads[0] != event_loop_thread


def test_explicit_fake_provider_options_are_validated() -> None:
    with pytest.raises(ValidationError):
        FakeProvider(fail_rate=1.1)
    with pytest.raises(ValidationError):
        FakeProvider(fail_rate=float("nan"))
    with pytest.raises(ValidationError):
        FakeProvider(fail_mode="400")
    with pytest.raises(ValidationError):
        FakeProvider(latency_ms=-1)


def test_tiktoken_encoder_is_initialized_once(monkeypatch: pytest.MonkeyPatch) -> None:
    _ORIGINAL_GET_ENCODER.cache_clear()
    calls: list[str] = []

    def encoding_for_model(model: str) -> Encoding:
        calls.append(model)
        return _CACHE_TEST_ENCODING

    monkeypatch.setattr(tiktoken, "encoding_for_model", encoding_for_model)
    try:
        first = _ORIGINAL_GET_ENCODER()
        second = _ORIGINAL_GET_ENCODER()

        assert first is second
        assert calls == ["gpt-4o-mini"]
    finally:
        _ORIGINAL_GET_ENCODER.cache_clear()


async def test_seeded_failure_rate_can_recover_for_a_later_attempt() -> None:
    provider = FakeProvider(fail_rate=0.5, rng=random.Random(1))

    with pytest.raises(ProviderError):
        await provider.translate_chunk(make_request())

    result = await provider.translate_chunk(make_request())
    assert result.translations["block-1"] == "[de] Hello"
