"""Offline navigation, fake triage, and bounded Agents SDK adapter behavior."""

from __future__ import annotations

import asyncio
import json
import random
from types import SimpleNamespace
from typing import Any

import httpx2
import openai
import pytest
from agents import FunctionTool
from agents.exceptions import MaxTurnsExceeded, ModelBehaviorError
from agents.tool_context import ToolContext
from agents.usage import Usage
from openai.types.responses.response_usage import InputTokensDetails
from pydantic import SecretStr, ValidationError

from app.adapters.llm import triage_agent
from app.adapters.llm.fake_triage_agent import MAX_TERMS, FakeTriageAgent
from app.adapters.llm.triage_agent import (
    DEFAULT_MAX_TURNS,
    DEFAULT_TIMEOUT_SECONDS,
    MAX_READ_BLOCKS,
    MAX_SEARCH_RESULTS,
    MAX_SNIPPET_CHARS,
    MAX_TOOL_CALLS,
    MAX_TOOL_OUTPUT_CHARS,
    OpenAITriageAgent,
    read_blocks,
    search_blocks,
)
from app.config import Settings
from app.core.errors import ErrorCode, ProviderError
from app.core.models import (
    Block,
    DocumentIR,
    TranslationPlan,
    TriageAgentOutput,
    TriageResult,
    TriageStatus,
)


def _document(texts: list[str] | None = None, *, start_seq: int = 0) -> DocumentIR:
    return DocumentIR(
        id="document-1",
        filename="untrusted-name.docx",
        format="docx",
        size_bytes=123,
        blocks=[
            Block(
                id=f"block-{seq}",
                seq=seq,
                source_text=text,
                source_hash="untrusted-hash",
                format_metadata={"must_not_inspect": [None, {"secret_layout": object()}]},
            )
            for seq, text in enumerate(
                texts or ["Alpha report", "Beta glossary", "Conclusion"], start=start_seq
            )
        ],
    )


async def _invoke(tool: FunctionTool, document: DocumentIR, **arguments: object) -> str:
    serialized = json.dumps(arguments)
    context = ToolContext(
        context=document,
        tool_name=tool.name,
        tool_call_id="offline-tool-call",
        tool_arguments=serialized,
    )
    return await tool.on_invoke_tool(context, serialized)


def _output() -> TriageAgentOutput:
    return TriageAgentOutput(
        reasoning="The report and glossary at seq 0–1 are English business text.",
        plan=TranslationPlan(
            source_language="en", domain="business", register="neutral", terms=["Alpha"]
        ),
    )


async def test_read_blocks_uses_sequence_interval_and_returns_only_text_and_seq() -> None:
    document = _document(start_seq=10)
    document.blocks.reverse()

    result = json.loads(await _invoke(read_blocks, document, start_seq=11, count=2))

    assert result == [
        {"seq": 11, "source_text": "Beta glossary"},
        {"seq": 12, "source_text": "Conclusion"},
    ]
    assert await _invoke(read_blocks, document, start_seq=99, count=2) == "[]"


async def test_navigation_limits_read_count_snippet_size_and_serialized_output() -> None:
    document = _document(["\x01" * 10_000] * 30)

    serialized = await _invoke(read_blocks, document, start_seq=0, count=1_000_000)
    result = json.loads(serialized)

    assert 0 < len(result) <= MAX_READ_BLOCKS
    assert len(serialized) <= MAX_TOOL_OUTPUT_CHARS
    assert all(len(item["source_text"]) <= MAX_SNIPPET_CHARS for item in result)


async def test_search_is_case_insensitive_capped_and_shows_late_match() -> None:
    text = "prefix " * 500 + "GLOSSARY Alpha Beta"
    serialized = await _invoke(search_blocks, _document([text] * 30), keyword="glossary")
    result = json.loads(serialized)

    assert len(result) == MAX_SEARCH_RESULTS
    assert result[0]["seq"] == 0
    assert "GLOSSARY" in result[0]["source_text"]
    assert all(len(item["source_text"]) <= MAX_SNIPPET_CHARS for item in result)
    assert len(serialized) <= MAX_TOOL_OUTPUT_CHARS
    assert await _invoke(search_blocks, _document(), keyword="absent") == "[]"


@pytest.mark.parametrize("keyword", ["", "   ", "x" * 201])
async def test_search_rejects_empty_or_unbounded_keywords(keyword: str) -> None:
    with pytest.raises(ValueError):
        await _invoke(search_blocks, _document(), keyword=keyword)


@pytest.mark.parametrize("count", [0, -1])
async def test_read_rejects_nonpositive_count(count: int) -> None:
    with pytest.raises(ValueError):
        await _invoke(read_blocks, _document(), start_seq=0, count=count)


def test_installed_sdk_schema_excludes_context_and_metadata() -> None:
    assert set(read_blocks.params_json_schema["properties"]) == {"start_seq", "count"}
    assert set(search_blocks.params_json_schema["properties"]) == {"keyword"}
    for tool in (read_blocks, search_blocks):
        assert tool.params_json_schema["additionalProperties"] is False
        assert "format_metadata" not in json.dumps(tool.params_json_schema)


async def test_per_run_navigation_budget_bounds_parallel_or_repeated_calls() -> None:
    budget = triage_agent._NavigationBudget()
    read, _ = triage_agent._navigation_tools(budget)
    for _ in range(MAX_TOOL_CALLS):
        await _invoke(read, _document(), start_seq=0, count=1)
    with pytest.raises(ProviderError) as raised:
        await _invoke(read, _document(), start_seq=0, count=1)
    assert raised.value.error_code == ErrorCode.PROVIDER_INVALID_RESPONSE
    assert budget.successful_reads == MAX_TOOL_CALLS


async def test_fake_plan_is_repeatable_and_uses_ordered_unique_capitalized_terms() -> None:
    document = _document(["Beta Alpha Beta", "Gamma delta"])
    document.blocks.reverse()
    fake = FakeTriageAgent(
        settings=Settings(openai_model="configured-model"), fail_rate=0.0, latency_ms=0
    )

    first = await fake.analyze(document)
    second = await fake.analyze(document)

    assert first == second
    assert isinstance(first, TriageResult)
    assert first.model == "configured-model"
    assert (first.tokens_in, first.tokens_out, first.cached_tokens_in, first.requests) == (
        173,
        29,
        61,
        3,
    )
    assert first.plan.source_language == "en"
    assert first.plan.domain == "general"
    assert first.plan.register == "neutral"
    assert first.plan.terms == ["Beta", "Alpha", "Gamma"]
    assert first.plan.warnings == []
    assert first.plan.triage_status == TriageStatus.OK
    words = [f"Term{chr(97 + index // 26)}{chr(97 + index % 26)}" for index in range(100)]
    many = await fake.analyze(_document([" ".join(words)]))
    assert many.plan.terms == words[:MAX_TERMS]


@pytest.mark.parametrize(
    ("text", "language"),
    [("English report", "en"), ("Український звіт", "uk"), ("Русский отчёт", "ru")],
)
async def test_fake_limited_language_detection(text: str, language: str) -> None:
    result = await FakeTriageAgent(fail_rate=0.0).analyze(_document([text]))
    assert result.plan.source_language == language


@pytest.mark.parametrize(
    ("mode", "code"),
    [
        ("429", ErrorCode.PROVIDER_RATE_LIMIT),
        ("500", ErrorCode.PROVIDER_SERVER_ERROR),
        ("timeout", ErrorCode.PROVIDER_TIMEOUT),
    ],
)
async def test_fake_reuses_settings_failure_injection(mode: str, code: ErrorCode) -> None:
    fake = FakeTriageAgent(settings=Settings(fake_fail_rate=1.0, fake_fail_mode=mode))
    with pytest.raises(ProviderError) as raised:
        await fake.analyze(_document())
    assert raised.value.error_code == code


async def test_fake_explicit_zero_overrides_fault_and_latency_settings() -> None:
    fake = FakeTriageAgent(
        settings=Settings(fake_fail_rate=1.0, fake_latency_ms=500), fail_rate=0.0, latency_ms=0
    )
    assert (await fake.analyze(_document())).plan.triage_status == TriageStatus.OK
    assert fake.config.latency_ms == 0


async def test_fake_latency_does_not_block_and_cancellation_propagates() -> None:
    task = asyncio.create_task(FakeTriageAgent(latency_ms=10_000).analyze(_document()))
    await asyncio.sleep(0)
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_fake_seeded_failure_can_succeed_on_later_attempt() -> None:
    fake = FakeTriageAgent(fail_rate=0.5, rng=random.Random(1))
    with pytest.raises(ProviderError):
        await fake.analyze(_document())
    assert (await fake.analyze(_document())).plan.triage_status == TriageStatus.OK


@pytest.mark.parametrize(
    "options",
    [{"fail_rate": 2.0}, {"fail_rate": float("nan")}, {"fail_mode": "400"}, {"latency_ms": -1}],
)
def test_fake_options_are_validated(options: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        FakeTriageAgent(**options)


async def test_openai_run_passes_navigation_context_bounds_and_disabled_tracing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def run(agent: Any, **kwargs: Any) -> object:
        captured.update(kwargs)
        captured["agent"] = agent
        await _invoke(agent.tools[0], kwargs["context"].context, start_seq=10, count=1)
        return SimpleNamespace(final_output=_output(), context_wrapper=SimpleNamespace())

    monkeypatch.setattr(triage_agent.Runner, "run", run)
    document = _document(start_seq=10)
    adapter = OpenAITriageAgent(settings=Settings(openai_model="configured-model"), client=object())

    result = await adapter.analyze(document)

    assert result.plan == _output().plan
    assert result.model == "configured-model"
    assert (result.tokens_in, result.tokens_out, result.cached_tokens_in, result.requests) == (
        0,
        0,
        0,
        0,
    )
    assert captured["context"].context is document
    assert captured["max_turns"] == DEFAULT_MAX_TURNS
    assert captured["run_config"].tracing_disabled is True
    assert captured["run_config"].trace_include_sensitive_data is False
    prompt = json.loads(captured["input"])
    assert prompt["block_count"] == 3
    assert (prompt["first_seq"], prompt["last_seq"]) == (10, 12)
    assert "untrusted" not in captured["input"]
    agent = captured["agent"]
    assert agent.model.model == "configured-model"
    assert agent.output_type is TriageAgentOutput
    assert agent.model_settings.tool_choice == "required"
    assert agent.model_settings.parallel_tool_calls is False
    assert agent.model_settings.retry.max_retries == 0
    assert agent.reset_tool_choice is True
    assert [tool.name for tool in agent.tools] == ["read_blocks", "search_blocks"]
    assert "private" in agent.instructions


@pytest.mark.parametrize("final_output", [_output(), {"plan": {}}, "invalid", None])
async def test_openai_requires_real_nonempty_document_navigation(
    monkeypatch: pytest.MonkeyPatch, final_output: object
) -> None:
    async def run(*args: Any, **kwargs: Any) -> object:
        return SimpleNamespace(final_output=final_output)

    monkeypatch.setattr(triage_agent.Runner, "run", run)
    with pytest.raises(ProviderError) as raised:
        await OpenAITriageAgent(client=object()).analyze(_document())
    assert raised.value.error_code == ErrorCode.PROVIDER_INVALID_RESPONSE


async def test_openai_rejects_degraded_provider_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    output = _output()
    output.plan.triage_status = TriageStatus.DEGRADED

    async def run(agent: Any, **kwargs: Any) -> object:
        await _invoke(agent.tools[0], kwargs["context"], start_seq=0, count=1)
        return SimpleNamespace(final_output=output)

    monkeypatch.setattr(triage_agent.Runner, "run", run)
    with pytest.raises(ProviderError) as raised:
        await OpenAITriageAgent(client=object()).analyze(_document())
    assert raised.value.error_code == ErrorCode.PROVIDER_INVALID_RESPONSE


def _aggregated_usage() -> Usage:
    usage = Usage()
    usage.add(
        Usage(
            requests=1,
            input_tokens=100,
            input_tokens_details=InputTokensDetails(cached_tokens=20, cache_write_tokens=0),
            output_tokens=11,
            total_tokens=111,
        )
    )
    usage.add(
        Usage(
            requests=1,
            input_tokens=150,
            input_tokens_details=InputTokensDetails(cached_tokens=30, cache_write_tokens=0),
            output_tokens=17,
            total_tokens=167,
        )
    )
    return usage


async def test_openai_returns_aggregated_usage_across_multiple_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aggregate = _aggregated_usage()

    async def run(*args: Any, **kwargs: Any) -> object:
        await _invoke(args[0].tools[0], kwargs["context"].context, start_seq=0, count=1)
        return SimpleNamespace(
            final_output=_output(), context_wrapper=SimpleNamespace(usage=aggregate)
        )

    monkeypatch.setattr(triage_agent.Runner, "run", run)
    result = await OpenAITriageAgent(
        settings=Settings(openai_model="configured-model"), client=object()
    ).analyze(_document())

    assert (result.tokens_in, result.tokens_out, result.cached_tokens_in, result.requests) == (
        250,
        28,
        50,
        2,
    )


async def test_openai_retains_usage_when_runner_fails_after_a_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aggregate = _aggregated_usage()

    async def run(*args: Any, **kwargs: Any) -> object:
        kwargs["context"].usage.add(aggregate)
        raise RuntimeError("sensitive provider details")

    monkeypatch.setattr(triage_agent.Runner, "run", run)
    with pytest.raises(ProviderError) as raised:
        await OpenAITriageAgent(
            settings=Settings(openai_model="configured-model"), client=object()
        ).analyze(_document())

    assert raised.value.error_code == ErrorCode.PROVIDER_INVALID_RESPONSE
    assert raised.value.model == "configured-model"
    assert (raised.value.tokens_in, raised.value.tokens_out) == (250, 28)
    assert raised.value.cached_tokens_in == 50
    assert raised.value.requests == 2


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (
            openai.APITimeoutError(request=httpx2.Request("POST", "https://example.invalid")),
            ErrorCode.PROVIDER_TIMEOUT,
        ),
        (
            openai.APIConnectionError(request=httpx2.Request("POST", "https://example.invalid")),
            ErrorCode.PROVIDER_CONNECTION,
        ),
        (ModelBehaviorError("sensitive provider details"), ErrorCode.PROVIDER_INVALID_RESPONSE),
        (MaxTurnsExceeded("sensitive provider details"), ErrorCode.PROVIDER_INVALID_RESPONSE),
        (RuntimeError("sensitive provider details"), ErrorCode.PROVIDER_INVALID_RESPONSE),
    ],
)
async def test_openai_maps_sdk_errors_to_safe_catalog(
    monkeypatch: pytest.MonkeyPatch, error: Exception, code: ErrorCode
) -> None:
    async def run(*args: Any, **kwargs: Any) -> object:
        raise error

    monkeypatch.setattr(triage_agent.Runner, "run", run)
    with pytest.raises(ProviderError) as raised:
        await OpenAITriageAgent(client=object()).analyze(_document())
    assert raised.value.error_code == code
    assert "sensitive" not in str(raised.value)
    assert raised.value.__suppress_context__ is True


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, ErrorCode.PROVIDER_BAD_REQUEST),
        (401, ErrorCode.PROVIDER_AUTH_ERROR),
        (403, ErrorCode.PROVIDER_AUTH_ERROR),
        (408, ErrorCode.PROVIDER_TIMEOUT),
        (429, ErrorCode.PROVIDER_RATE_LIMIT),
        (500, ErrorCode.PROVIDER_SERVER_ERROR),
    ],
)
async def test_openai_maps_http_errors(
    monkeypatch: pytest.MonkeyPatch, status: int, code: ErrorCode
) -> None:
    response = httpx2.Response(status, request=httpx2.Request("POST", "https://example.invalid"))
    error_type = {
        400: openai.BadRequestError,
        401: openai.AuthenticationError,
        429: openai.RateLimitError,
    }.get(status, openai.APIStatusError)
    error = error_type("sensitive details", response=response, body=None)

    async def run(*args: Any, **kwargs: Any) -> object:
        raise error

    monkeypatch.setattr(triage_agent.Runner, "run", run)
    with pytest.raises(ProviderError) as raised:
        await OpenAITriageAgent(client=object()).analyze(_document())
    assert raised.value.error_code == code
    assert "sensitive" not in str(raised.value)


async def test_openai_timeout_cancels_runner_and_external_cancellation_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancelled: list[bool] = []

    async def run(*args: Any, **kwargs: Any) -> object:
        kwargs["context"].usage.add(_aggregated_usage())
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.append(True)

    monkeypatch.setattr(triage_agent.Runner, "run", run)
    with pytest.raises(ProviderError) as raised:
        await OpenAITriageAgent(client=object(), timeout_seconds=0.01).analyze(_document())
    assert raised.value.error_code == ErrorCode.PROVIDER_TIMEOUT
    assert (raised.value.tokens_in, raised.value.tokens_out) == (250, 28)
    assert raised.value.cached_tokens_in == 50
    assert raised.value.requests == 2
    assert cancelled == [True]

    task = asyncio.create_task(OpenAITriageAgent(client=object()).analyze(_document()))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled == [True, True]


async def test_missing_key_is_lazy_catalogued_failure() -> None:
    adapter = OpenAITriageAgent(settings=Settings(openai_api_key=SecretStr("")))
    with pytest.raises(ProviderError) as raised:
        await adapter.analyze(_document())
    assert raised.value.error_code == ErrorCode.PROVIDER_AUTH_ERROR
    await adapter.aclose()


async def test_created_client_disables_retries_and_is_closed_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    closed: list[bool] = []

    class Client:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        async def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(openai, "AsyncOpenAI", Client)
    adapter = OpenAITriageAgent(settings=Settings(openai_api_key=SecretStr("offline-key")))
    assert adapter._get_client("model") is adapter._get_client("model")
    assert captured["max_retries"] == 0
    assert captured["timeout"] == DEFAULT_TIMEOUT_SECONDS
    await adapter.aclose()
    await adapter.aclose()
    assert closed == [True]


async def test_injected_client_remains_caller_owned() -> None:
    class Client:
        async def close(self) -> None:
            pytest.fail("injected client should remain caller-owned")

    await OpenAITriageAgent(client=Client()).aclose()


@pytest.mark.parametrize(
    "options", [{"timeout_seconds": 0}, {"timeout_seconds": float("inf")}, {"max_turns": 0}]
)
def test_openai_bounds_are_validated(options: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        OpenAITriageAgent(**options)


@pytest.mark.live
async def test_live_triage_produces_plan_after_sdk_navigation() -> None:
    settings = Settings()
    if not settings.openai_api_key.get_secret_value():
        pytest.skip("Live triage requires configured OpenAI credentials")
    adapter = OpenAITriageAgent(settings=settings)
    try:
        plan = await adapter.analyze(
            _document(
                [
                    "Annual Business Report",
                    "Revenue rose by 10 percent.",
                    "Glossary: Revenue means income from sales.",
                ]
            )
        )
    finally:
        await adapter.aclose()
    assert isinstance(plan, TriageResult)
    assert plan.plan.source_language.lower() in {"en", "eng", "english"}
    assert plan.plan.triage_status == TriageStatus.OK
