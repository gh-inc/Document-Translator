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
from structlog.testing import capture_logs

from app.adapters.llm import triage_agent
from app.adapters.llm.fake_triage_agent import MAX_TERMS, FakeTriageAgent
from app.adapters.llm.triage_agent import (
    _INSTRUCTIONS,
    _OUTLINE_HEAD_CHARS,
    _OUTLINE_MAX_BLOCKS,
    DEFAULT_MAX_TURNS,
    DEFAULT_TIMEOUT_SECONDS,
    MAX_READ_BLOCKS,
    MAX_SEARCH_RESULTS,
    MAX_SNIPPET_CHARS,
    MAX_TOOL_OUTPUT_CHARS,
    OpenAITriageAgent,
    _document_outline,
    read_blocks,
    search_blocks,
)
from app.adapters.llm.triage_runtime import prepare_triage
from app.adapters.persistence.database import SqliteConnectionFactory, transaction
from app.adapters.persistence.repositories import SqliteDocumentRepository
from app.config import Settings
from app.core.errors import ErrorCode, ProviderError, TriageTerminalError
from app.core.models import (
    Block,
    DocumentIR,
    DocumentStatus,
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


def test_outline_is_bounded_edge_weighted_and_uses_sorted_sparse_sequences() -> None:
    document = _document([f"Paragraph {i} " + "x" * 100 for i in range(500)])
    for index, block in enumerate(document.blocks):
        block.seq = index * 3
    document.blocks.reverse()

    outline = _document_outline(document)

    assert outline["block_count"] == 500
    assert (outline["first_seq"], outline["last_seq"]) == (0, 1497)
    blocks = outline["blocks"]
    assert len(blocks) == _OUTLINE_MAX_BLOCKS == 60
    assert [item["seq"] for item in blocks] == [
        *(index * 3 for index in range(30)),
        *(index * 3 for index in range(470, 500)),
    ]
    assert all(len(item["head"]) <= _OUTLINE_HEAD_CHARS == 80 for item in blocks)
    assert "must_not_inspect" not in json.dumps(outline)


def test_outline_reports_script_hints_from_entire_document() -> None:
    document = _document(["Hello", "Привет", "مرحبا", "你好"])
    outline = _document_outline(document)
    assert outline["scripts"] == {"latin": 5, "cyrillic": 6, "arabic": 5, "cjk": 2}
    assert [item["seq"] for item in outline["blocks"]] == [0, 1, 2, 3]


def test_instructions_name_only_existing_tools_and_use_outline() -> None:
    assert "summaries" not in _INSTRUCTIONS
    assert "glossary sections" not in _INSTRUCTIONS
    assert "read_blocks" in _INSTRUCTIONS and "search_blocks" in _INSTRUCTIONS
    assert "outline" in _INSTRUCTIONS
    assert "limited number of turns" in _INSTRUCTIONS


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


async def test_navigation_budget_tracks_successful_reads() -> None:
    budget = triage_agent._NavigationBudget()
    read, _ = triage_agent._navigation_tools(budget)
    await _invoke(read, _document(), start_seq=0, count=1)
    assert budget.successful_reads == 1


async def test_tool_calls_are_logged_without_document_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run(agent: Any, **kwargs: Any) -> object:
        document = kwargs["context"].context
        await _invoke(agent.tools[0], document, start_seq=0, count=2)
        await _invoke(agent.tools[1], document, keyword="Alpha")
        return SimpleNamespace(final_output=_output())

    monkeypatch.setattr(triage_agent.Runner, "run", run)
    with capture_logs() as logs:
        await OpenAITriageAgent(client=object()).analyze(_document(["Alpha", "Beta"]))

    calls = [log for log in logs if log["event"] == "triage_tool_call"]
    assert calls == [
        {
            "event": "triage_tool_call",
            "log_level": "info",
            "tool": "read_blocks",
            "start_seq": 0,
            "count": 2,
            "returned": 2,
        },
        {
            "event": "triage_tool_call",
            "log_level": "info",
            "tool": "search_blocks",
            "keyword_length": 5,
            "returned": 1,
        },
    ]
    assert "Alpha" not in json.dumps(logs)
    assert "Beta" not in json.dumps(logs)
    assert "keyword" not in calls[1]
    assert "source_text" not in json.dumps(logs)


async def test_fake_plan_is_repeatable_and_uses_ordered_unique_capitalized_terms() -> None:
    document = _document(["Beta Alpha Beta", "Gamma delta"])
    document.blocks.reverse()
    fake = FakeTriageAgent(
        settings=Settings(openai_model="bulk-model", triage_model="configured-model"),
        fail_rate=0.0,
        latency_ms=0,
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
    adapter = OpenAITriageAgent(
        settings=Settings(openai_model="bulk-model", triage_model="configured-model"),
        client=object(),
    )

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
    assert prompt["blocks"] == [
        {"seq": 10, "head": "Alpha report"},
        {"seq": 11, "head": "Beta glossary"},
        {"seq": 12, "head": "Conclusion"},
    ]
    assert "text_characters" not in prompt
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


@pytest.mark.parametrize(
    ("configured", "expected"),
    [("  gpt-4o  ", "gpt-4o"), ("   ", "gpt-6-luna")],
)
async def test_triage_model_is_trimmed_or_uses_its_own_fallback(
    monkeypatch: pytest.MonkeyPatch, configured: str, expected: str
) -> None:
    async def run(agent: Any, **kwargs: Any) -> object:
        assert agent.model.model == expected
        await _invoke(agent.tools[0], kwargs["context"].context, start_seq=0, count=1)
        return SimpleNamespace(final_output=_output())

    monkeypatch.setattr(triage_agent.Runner, "run", run)
    settings = Settings(openai_model="bulk-model", triage_model=configured)
    real = await OpenAITriageAgent(settings=settings, client=object()).analyze(_document())
    fake = await FakeTriageAgent(settings=settings, fail_rate=0.0).analyze(_document())
    assert real.model == fake.model == expected


@pytest.mark.parametrize(
    "model",
    ["gpt-6-luna", "gpt-5.6-luna", "gpt-4o", "gpt-4o-mini"],
)
async def test_triage_model_settings_reach_chat_completions_wire(model: str) -> None:
    requests: list[dict[str, Any]] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content))
        return httpx2.Response(
            400,
            request=request,
            json={"error": {"message": "offline stop", "type": "invalid_request_error"}},
        )

    http_client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    sdk_client = openai.AsyncOpenAI(
        api_key="offline-test-key",
        base_url="https://offline.invalid/v1",
        max_retries=0,
        http_client=http_client,
    )
    sdk_client._platform = "Linux"
    try:
        with pytest.raises(ProviderError) as raised:
            await OpenAITriageAgent(
                settings=Settings(triage_model=model), client=sdk_client
            ).analyze(_document())
    finally:
        await sdk_client.close()

    assert raised.value.error_code == ErrorCode.PROVIDER_BAD_REQUEST
    assert len(requests) == 1
    body = requests[0]
    assert body["model"] == model
    assert body["tool_choice"] == "required"
    assert body["parallel_tool_calls"] is False
    assert {tool["function"]["name"] for tool in body["tools"]} == {
        "read_blocks",
        "search_blocks",
    }
    if model in {"gpt-6-luna", "gpt-5.6-luna"}:
        assert body["reasoning_effort"] == "none"
        assert body["max_completion_tokens"] == 2000
        assert "max_tokens" not in body
    else:
        assert body["max_tokens"] == 2000
        assert "max_completion_tokens" not in body
        assert "reasoning_effort" not in body


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
        settings=Settings(triage_model="configured-model"), client=object()
    ).analyze(_document())

    assert (result.tokens_in, result.tokens_out, result.cached_tokens_in, result.requests) == (
        250,
        28,
        50,
        2,
    )


async def test_turn_budget_exhaustion_is_terminal_and_logged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aggregate = _aggregated_usage()

    async def run(agent: Any, **kwargs: Any) -> object:
        document = kwargs["context"].context
        await _invoke(agent.tools[0], document, start_seq=0, count=2)
        await _invoke(agent.tools[0], document, start_seq=99, count=2)
        await _invoke(agent.tools[1], document, keyword="Alpha")
        kwargs["context"].usage.add(aggregate)
        raise MaxTurnsExceeded("sensitive run details")

    monkeypatch.setattr(triage_agent.Runner, "run", run)
    with capture_logs() as logs, pytest.raises(TriageTerminalError) as raised:
        await OpenAITriageAgent(
            settings=Settings(triage_model="configured-model"),
            client=object(),
            max_turns=1,
        ).analyze(_document())

    assert raised.value.error_code is ErrorCode.PROVIDER_INVALID_RESPONSE
    assert raised.value.retryable is True
    assert raised.value.terminal is True
    assert raised.value.model == "configured-model"
    assert (raised.value.tokens_in, raised.value.tokens_out) == (250, 28)
    assert raised.value.cached_tokens_in == 50
    assert raised.value.requests == 2
    exhausted = next(log for log in logs if log["event"] == "triage_turn_budget_exhausted")
    assert exhausted["tool_calls"] == 3
    assert exhausted["read_calls"] == 2
    assert exhausted["search_calls"] == 1
    assert "Alpha" not in json.dumps(logs)
    assert "sensitive" not in json.dumps(logs)


async def test_triage_service_guard_stays_above_configured_agent_timeout(
    tmp_path: Any,
) -> None:
    settings = Settings(
        database_path=tmp_path / "triage-guard.db",
        upload_storage_path=tmp_path / "uploads",
        output_storage_path=tmp_path / "output",
        llm_provider="fake",
        triage_timeout_seconds=12.0,
    )
    connection = await SqliteConnectionFactory(settings.database_path).create()
    try:
        repository = SqliteDocumentRepository(connection)
        async with transaction(connection):
            await repository.create_document("guard", "guard.pdf", "pdf", 1, "/unused")
            await repository.create_blocks(
                "guard",
                [Block(id="guard-block", seq=0, source_text="Hello", source_hash="hash")],
            )
            await repository.update_document_status("guard", DocumentStatus.ANALYZING)
    finally:
        await connection.close()

    claim = await prepare_triage("guard", settings)
    assert claim is not None
    try:
        assert claim._service._attempt_timeout_seconds == 17.0
    finally:
        await claim.close()


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
            settings=Settings(triage_model="configured-model"), client=object()
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
