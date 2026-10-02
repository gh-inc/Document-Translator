"""Offline behavior tests for the OpenAI adapter."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from typing import Any

import httpx2
import openai
import pytest
from pydantic import SecretStr

from app.adapters.llm.openai_provider import (
    DEFAULT_TIMEOUT_SECONDS,
    OpenAIProvider,
    TranslationsResponse,
)
from app.config import Settings
from app.core.errors import ErrorCode, ProviderError
from app.core.models import Block, ChunkRequest, TranslationPlan


def _block(block_id: str, seq: int, text: str) -> Block:
    return Block(
        id=block_id,
        seq=seq,
        source_text=text,
        source_hash=f"hash-{block_id}",
        format_metadata={"must_not_send": "secret layout data"},
    )


def _request(*, model: str = "request-model") -> ChunkRequest:
    return ChunkRequest(
        chunk_id="chunk-1",
        blocks=[_block("block-1", 1, 'Text with "quotes" and\nnewlines.')],
        context_before=[_block("before", 0, "Previous paragraph")],
        context_after=[_block("after", 2, "Following paragraph")],
        target_language='de "formal"',
        plan=TranslationPlan(
            source_language="en",
            domain='contracts "and" law',
            register="formal",
            terms=["agreement"],
            warnings=["Preserve quoted terms"],
        ),
        glossary={"agreement": '"Vereinbarung"\nexactly'},
        model=model,
    )


def _completion(
    parsed: object | None,
    *,
    refusal: str | None = None,
    usage: object | None = SimpleNamespace(prompt_tokens=11, completion_tokens=7),
) -> object:
    message = SimpleNamespace(parsed=parsed, refusal=refusal)
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(choices=[choice], usage=usage)


class _FakeCompletions:
    def __init__(self, completion: object | None = None, error: Exception | None = None) -> None:
        self.completion = completion
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def parse(self, **kwargs: Any) -> object:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        assert self.completion is not None
        return self.completion


class _FakeClient:
    def __init__(self, completions: _FakeCompletions) -> None:
        self.beta = SimpleNamespace(
            chat=SimpleNamespace(completions=completions),
        )


def _provider(
    response: dict[str, object] | None = None,
    *,
    completion: object | None = None,
    error: Exception | None = None,
    settings: Settings | None = None,
) -> tuple[OpenAIProvider, _FakeCompletions]:
    parsed = TranslationsResponse.model_validate(response) if response is not None else None
    fake_completions = _FakeCompletions(completion or _completion(parsed), error)
    return OpenAIProvider(client=_FakeClient(fake_completions), settings=settings), fake_completions


async def test_translates_chunk_with_strict_schema_and_usage() -> None:
    provider, fake = _provider(
        {"translations": [{"block_id": "block-1", "translated_text": "Hallo"}]}
    )

    result = await provider.translate_chunk(_request())

    assert result.translations == {"block-1": "Hallo"}
    assert (result.tokens_in, result.tokens_out, result.model) == (11, 7, "request-model")
    call = fake.calls[0]
    assert call["response_format"] is TranslationsResponse
    assert call["model"] == "request-model"


async def test_prompt_includes_source_context_but_excludes_format_metadata() -> None:
    provider, fake = _provider(
        {"translations": [{"block_id": "block-1", "translated_text": "Hallo"}]}
    )
    await provider.translate_chunk(_request())
    messages = fake.calls[0]["messages"]
    system_prompt = messages[0]["content"]
    user_prompt = messages[1]["content"]

    assert '"Previous paragraph"' in user_prompt
    assert '"Following paragraph"' in user_prompt
    assert '"Text with \\"quotes\\" and\\nnewlines."' in user_prompt
    assert "must_not_send" not in user_prompt
    assert "secret layout data" not in user_prompt
    assert 'contracts \\"and\\" law' in system_prompt
    assert "must_not_send" not in system_prompt
    glossary_json = next(
        line.partition(": ")[2]
        for line in system_prompt.splitlines()
        if line.startswith("Glossary JSON:")
    )
    assert json.loads(glossary_json) == {"agreement": '"Vereinbarung"\nexactly'}
    payload = json.loads(user_prompt)
    assert set(payload["blocks_to_translate"][0]) == {"seq", "block_id", "source_text"}


@pytest.mark.parametrize(
    "items",
    [
        [],
        [
            {"block_id": "block-1", "translated_text": "one"},
            {"block_id": "block-1", "translated_text": "two"},
        ],
        [{"block_id": "other", "translated_text": "extra"}],
        [
            {"block_id": "block-1", "translated_text": "Hallo"},
            {"block_id": "other", "translated_text": "extra"},
        ],
    ],
)
async def test_missing_extra_or_duplicate_ids_are_retryable(items: list[dict[str, str]]) -> None:
    provider, _ = _provider({"translations": items})

    with pytest.raises(ProviderError) as raised:
        await provider.translate_chunk(_request())

    assert raised.value.error_code == ErrorCode.PROVIDER_INVALID_RESPONSE
    assert raised.value.retryable is True
    assert (raised.value.tokens_in, raised.value.tokens_out) == (11, 7)


async def test_duplicate_requested_ids_are_fatal_and_rejected_before_call() -> None:
    provider, _ = _provider({"translations": [{"block_id": "block-1", "translated_text": "Hallo"}]})
    request = _request().model_copy(
        update={"blocks": [_block("block-1", 1, "a"), _block("block-1", 2, "b")]}
    )

    with pytest.raises(ProviderError) as raised:
        await provider.translate_chunk(request)

    assert raised.value.error_code == ErrorCode.PROVIDER_BAD_REQUEST
    assert raised.value.retryable is False


async def test_unexpected_sdk_error_is_suppressed_from_public_exception_chain() -> None:
    provider, _ = _provider(error=openai.OpenAIError("private SDK details"))

    with pytest.raises(ProviderError) as raised:
        await provider.translate_chunk(_request())

    assert raised.value.error_code == ErrorCode.PROVIDER_INVALID_RESPONSE
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True
    assert "private SDK details" not in str(raised.value)


async def test_refusal_is_fatal_and_retains_usage() -> None:
    completion = _completion(None, refusal="private provider text")
    provider, _ = _provider(completion=completion)

    with pytest.raises(ProviderError) as raised:
        await provider.translate_chunk(_request())

    assert raised.value.error_code == ErrorCode.PROVIDER_REFUSAL
    assert raised.value.retryable is False
    assert (raised.value.tokens_in, raised.value.tokens_out) == (11, 7)
    assert "private provider text" not in str(raised.value)


async def test_missing_parsed_response_is_retryable_and_retains_usage() -> None:
    provider, _ = _provider(completion=_completion(None))

    with pytest.raises(ProviderError) as raised:
        await provider.translate_chunk(_request())

    assert raised.value.error_code == ErrorCode.PROVIDER_INVALID_RESPONSE
    assert (raised.value.tokens_in, raised.value.tokens_out) == (11, 7)


@pytest.mark.parametrize(
    ("exception_factory", "expected"),
    [
        (lambda: openai.APITimeoutError(request=object()), ErrorCode.PROVIDER_TIMEOUT),
        (lambda: openai.APIConnectionError(request=object()), ErrorCode.PROVIDER_CONNECTION),
        (
            lambda: openai.RateLimitError(
                "private provider text", response=_status_response(429), body={}
            ),
            ErrorCode.PROVIDER_RATE_LIMIT,
        ),
        (
            lambda: openai.APIStatusError(
                "private provider text", response=_status_response(503), body={}
            ),
            ErrorCode.PROVIDER_SERVER_ERROR,
        ),
        (
            lambda: openai.APIStatusError(
                "private provider text", response=_status_response(408), body={}
            ),
            ErrorCode.PROVIDER_TIMEOUT,
        ),
        (
            lambda: openai.BadRequestError(
                "private provider text", response=_status_response(400), body={}
            ),
            ErrorCode.PROVIDER_BAD_REQUEST,
        ),
        (
            lambda: openai.AuthenticationError(
                "private provider text", response=_status_response(401), body={}
            ),
            ErrorCode.PROVIDER_AUTH_ERROR,
        ),
    ],
)
async def test_sdk_errors_are_mapped_to_safe_catalog_errors(
    exception_factory: Any, expected: ErrorCode
) -> None:
    provider, _ = _provider(error=exception_factory())

    with pytest.raises(ProviderError) as raised:
        await provider.translate_chunk(_request())

    assert raised.value.error_code == expected
    assert "private provider text" not in str(raised.value)


def _status_response(status_code: int) -> Any:
    return SimpleNamespace(
        request=object(),
        status_code=status_code,
        headers={"x-request-id": "request-id"},
    )


async def test_truncated_sdk_completion_is_retryable_and_retains_usage() -> None:
    sdk_completion = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=13, completion_tokens=4))
    provider, _ = _provider(
        error=openai.LengthFinishReasonError(completion=sdk_completion),
    )

    with pytest.raises(ProviderError) as raised:
        await provider.translate_chunk(_request())

    assert raised.value.error_code == ErrorCode.PROVIDER_INVALID_RESPONSE
    assert (raised.value.tokens_in, raised.value.tokens_out) == (13, 4)


async def test_missing_usage_is_invalid_response() -> None:
    provider, _ = _provider(
        {"translations": [{"block_id": "block-1", "translated_text": "Hallo"}]},
        completion=_completion(
            TranslationsResponse.model_validate(
                {"translations": [{"block_id": "block-1", "translated_text": "Hallo"}]}
            ),
            usage=None,
        ),
    )
    # The completion is parseable, but usage is part of the adapter contract and
    # missing usage should not silently look like a free successful request.
    with pytest.raises(ProviderError) as raised:
        await provider.translate_chunk(_request())

    assert raised.value.error_code == ErrorCode.PROVIDER_INVALID_RESPONSE


async def test_empty_request_model_falls_back_to_settings_model() -> None:
    settings = Settings(openai_model="configured-model")
    provider, fake = _provider(
        {"translations": [{"block_id": "block-1", "translated_text": "Hallo"}]},
        settings=settings,
    )

    result = await provider.translate_chunk(_request(model="  "))

    assert result.model == "configured-model"
    assert fake.calls[0]["model"] == "configured-model"


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_timeout_must_be_finite_and_positive(timeout: float) -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        OpenAIProvider(timeout_seconds=timeout)


async def test_unavailable_api_key_is_safe_auth_error() -> None:
    settings = Settings(openai_api_key=SecretStr(""))
    provider = OpenAIProvider(settings=settings)

    with pytest.raises(ProviderError) as raised:
        await provider.translate_chunk(_request())

    assert raised.value.error_code == ErrorCode.PROVIDER_AUTH_ERROR
    assert "api_key" not in str(raised.value)


def test_openai_client_disables_retries_and_uses_finite_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class Client:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        async def close(self) -> None:
            return None

    monkeypatch.setattr(openai, "AsyncOpenAI", Client)
    provider = OpenAIProvider(
        settings=Settings(openai_api_key=SecretStr("test-key")),
    )

    provider._get_client("request-model")

    assert captured["api_key"] == "test-key"
    assert captured["max_retries"] == 0
    assert captured["timeout"] == DEFAULT_TIMEOUT_SECONDS


def test_prompt_payloads_are_valid_json() -> None:
    provider, fake = _provider(
        {"translations": [{"block_id": "block-1", "translated_text": "Hallo"}]}
    )

    async def translate() -> None:
        await provider.translate_chunk(_request())

    import asyncio

    asyncio.run(translate())
    json.loads(fake.calls[0]["messages"][1]["content"])


async def test_installed_sdk_sends_strict_schema_through_mock_transport() -> None:
    requests: list[dict[str, Any]] = []
    response_content = json.dumps(
        {"translations": [{"block_id": "block-1", "translated_text": "Hallo"}]},
        separators=(",", ":"),
    )

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content))
        return httpx2.Response(
            200,
            request=request,
            json={
                "id": "chatcmpl-offline",
                "object": "chat.completion",
                "created": 1,
                "model": "request-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": response_content,
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 7,
                    "total_tokens": 18,
                },
            },
        )

    http_client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    sdk_client = openai.AsyncOpenAI(
        api_key="offline-test-key",
        base_url="https://offline.invalid/v1",
        max_retries=0,
        timeout=DEFAULT_TIMEOUT_SECONDS,
        http_client=http_client,
    )
    # The SDK lazily computes a diagnostic platform header in a worker thread.
    # This test isolates request serialization and runs in a constrained thread
    # pool, so prime that cached value as the installed SDK does after startup.
    sdk_client._platform = "Linux"
    provider = OpenAIProvider(client=sdk_client)
    try:
        result = await provider.translate_chunk(_request())
    finally:
        await sdk_client.close()

    assert result.translations == {"block-1": "Hallo"}
    assert len(requests) == 1
    request_body = requests[0]
    assert request_body["messages"][0]["role"] == "system"
    assert "must_not_send" not in json.dumps(request_body["messages"])
    json_schema = request_body["response_format"]["json_schema"]
    assert json_schema["strict"] is True
    root_schema = json_schema["schema"]
    assert root_schema["additionalProperties"] is False
    item_schema = root_schema["$defs"]["TranslationItem"]
    assert item_schema["additionalProperties"] is False
    assert item_schema["required"] == [
        "block_id",
        "translated_text",
    ]


async def test_installed_sdk_does_not_retry_server_errors() -> None:
    requests = 0

    async def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal requests
        requests += 1
        return httpx2.Response(
            500,
            request=request,
            json={"error": {"message": "private provider text", "type": "server_error"}},
        )

    http_client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    sdk_client = openai.AsyncOpenAI(
        api_key="offline-test-key",
        base_url="https://offline.invalid/v1",
        max_retries=0,
        timeout=DEFAULT_TIMEOUT_SECONDS,
        http_client=http_client,
    )
    sdk_client._platform = "Linux"
    provider = OpenAIProvider(client=sdk_client)
    try:
        with pytest.raises(ProviderError) as raised:
            await provider.translate_chunk(_request())
    finally:
        await sdk_client.close()

    assert requests == 1
    assert raised.value.error_code == ErrorCode.PROVIDER_SERVER_ERROR
    assert raised.value.__cause__ is None
    assert "private provider text" not in str(raised.value)


@pytest.mark.live
@pytest.mark.skipif(not os.getenv("OPENAI_API_KEY"), reason="OPENAI_API_KEY is not configured")
async def test_openai_provider_live_contract() -> None:
    from tests.adapters.llm.provider_contract import run_provider_smoke

    provider = OpenAIProvider()
    async with provider:
        await run_provider_smoke(provider)
