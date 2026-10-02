# Stage 2 — LLM Provider Layer Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement a testable `LLMProvider` port with `FakeProvider` (deterministic, configurable failures) and `OpenAIProvider` (real API), plus a `CostCalculator`. All real-provider calls remain opt-in via `@pytest.mark.live`.

**Architecture:**
- **Plain completions, not agents.** Bulk translation is a deterministic prompt mapped over chunks; the agent is reserved for triage (Stage 6).
- **OpenAI Structured Outputs.** Use `client.beta.chat.completions.parse(..., response_format=TranslationsResponse)` or `response_format={"type": "json_schema", ...}` to eliminate key-hallucination bugs.
- **Source-side context.** `ChunkRequest` carries optional `context_before` / `context_after` blocks; the provider includes them in the prompt but returns translations only for the chunk's own blocks.
- **Tiktoken caching.** `tiktoken.encoding_for_model` is expensive; the encoder is initialized once at module or class level and reused.
- **Honest cost.** `CostCalculator` uses a hardcoded per-model pricing table and token counts from `ChunkResult`.

**Tech Stack:** Python 3.12, `openai`, `tiktoken`, Pydantic v2.

**Current State:**
- `app/adapters/llm/pricing.py` is an unwired skeleton.
- `FakeProvider`, `OpenAIProvider`, and `triage_agent.py` do not exist yet.
- `ChunkRequest` currently has no fields for neighboring source-block context.

---

## Task 1: Extend `ChunkRequest` with Source-Side Context

**Files:**
- Modify: `app/core/models.py`
- Modify: `tests/test_core_models.py`

Add optional fields:

```python
context_before: list[Block] = Field(default_factory=list)
context_after: list[Block] = Field(default_factory=list)
```

Update `MODEL_FIXTURES` in `tests/test_core_models.py` so at least one fixture includes context blocks.

**Step 1: Write the failing test**

```python
def test_chunk_request_roundtrips_context_blocks() -> None:
    data = dict(dict(MODEL_FIXTURES)[ChunkRequest])
    data["context_before"] = [BLOCK_DATA]
    data["context_after"] = [BLOCK_DATA]
    instance = ChunkRequest.model_validate(data)
    assert ChunkRequest.model_validate_json(instance.model_dump_json()) == instance
```

Run: `pytest tests/test_core_models.py::test_chunk_request_roundtrips_context_blocks -v`
Expected: FAIL before implementation.

**Step 2: Add the fields to `ChunkRequest`**

**Step 3: Run tests**

Run: `pytest tests/test_core_models.py -v`
Expected: PASS.

---

## Task 2: Implement `FakeProvider`

**Files:**
- Create: `app/adapters/llm/fake_provider.py`

Behavior:
- Deterministic pseudo-translation: `"[{target_language}] {source_text}"` for each block.
- Token counting via `tiktoken`, but **cache the encoder** at module/class level.
  ```python
  _ENCODER = tiktoken.encoding_for_model("gpt-4o-mini")
  ```
- Configurable failure modes via constructor (fallback to env):
  - `fail_rate: float` (0.0–1.0)
  - `fail_mode: str` ∈ `{"429", "500", "timeout"}`
  - `latency_ms: int`
- Raise exceptions that match the shapes the worker will handle (timeout, retryable API errors, fatal API errors).

**Step 1: Write the failing test**

```python
async def test_fake_provider_translates_blocks() -> None:
    provider = FakeProvider()
    result = await provider.translate_chunk(ChunkRequest(...))
    assert result.translations == {"block-1": "[de] Hello"}
```

Run: `pytest tests/adapters/llm/test_fake_provider.py -v`
Expected: FAIL.

**Step 2: Implement `FakeProvider`**

**Step 3: Run tests**

Expected: PASS.

---

## Task 3: Implement `ModelCostCalculator`

**Files:**
- Modify: `app/adapters/llm/pricing.py`

Add a pricing table for at least:

```python
_PRICES: dict[str, dict[str, float]] = {
    "gpt-4o-mini": {"input": 0.150, "output": 0.600},
    "gpt-4o": {"input": 2.500, "output": 10.000},
}
```

Prices are per 1 million tokens. `estimate(model, tokens_in, tokens_out)` returns:

```python
((tokens_in * prices["input"]) + (tokens_out * prices["output"])) / 1_000_000
```

Raise `ValueError` for unknown models.

**Step 1: Write the failing test**

```python
def test_cost_calculator_for_known_model() -> None:
    calc = ModelCostCalculator()
    assert calc.estimate("gpt-4o-mini", 1_000_000, 1_000_000) == pytest.approx(0.75)
```

Run: `pytest tests/adapters/llm/test_pricing.py -v`
Expected: FAIL.

**Step 2: Implement `ModelCostCalculator`**

**Step 3: Run tests**

Expected: PASS.

---

## Task 4: Implement `OpenAIProvider`

**Files:**
- Create: `app/adapters/llm/openai_provider.py`

Define a Pydantic response schema:

```python
class TranslationsResponse(BaseModel):
    translations: dict[str, str]
```

Implementation behavior:
- Initialize `AsyncOpenAI(api_key=settings.openai_api_key.get_secret_value())`.
- Default model from `settings.openai_model` (`gpt-4o-mini`).
- Build system prompt from `TranslationPlan`, glossary, target language, and instructions.
- Build user prompt from `context_before`, chunk `blocks`, `context_after`.
- Call structured output API:
  ```python
  completion = await client.beta.chat.completions.parse(
      model=model,
      messages=[...],
      response_format=TranslationsResponse,
  )
  response = completion.choices[0].message.parsed
  ```
  If `parse` is unavailable in the installed SDK, fall back to:
  ```python
  response_format = {"type": "json_schema", "json_schema": {...}}
  ```
  and parse manually.
- **Missing block validation:** assert that every `block.id` from `ChunkRequest.blocks` is present in `response.translations`. If any are missing, raise a retryable error.
- Map errors:
  - `openai.APITimeoutError`, `openai.APIConnectionError`, `openai.RateLimitError`, 5xx status → retryable.
  - `openai.BadRequestError` (context length, malformed request) → fatal.
- Return `ChunkResult(translations, tokens_in, tokens_out, model)` using `completion.usage`.

**Step 1: Write the failing test**

Create a live test behind `@pytest.mark.live`:

```python
@pytest.mark.live
async def test_openai_provider_translates_chunk() -> None:
    provider = OpenAIProvider()
    result = await provider.translate_chunk(ChunkRequest(...))
    assert "block-1" in result.translations
    assert result.tokens_in > 0
```

Run without live marker: skipped.

**Step 2: Implement `OpenAIProvider`**

**Step 3: Run live test (opt-in, costs money)**

```bash
pytest tests/adapters/llm/test_openai_provider.py -v -m live
```

Expected: PASS if `OPENAI_API_KEY` is set.

---

## Task 5: Port Test-Kit

**Files:**
- Create: `tests/adapters/llm/test_llm_provider.py`

Write a reusable async test function that exercises any `LLMProvider`:

```python
async def run_provider_smoke(provider: LLMProvider) -> None:
    result = await provider.translate_chunk(ChunkRequest(...))
    assert set(result.translations.keys()) == {"block-1", "block-2"}
    assert result.tokens_in > 0
    assert result.tokens_out > 0
    assert result.model
```

Call it for `FakeProvider` in normal tests and for `OpenAIProvider` in live tests.

**Exit criteria:** normal suite passes; live suite is opt-in.

---

## Task 6: Update Settings and `.env.example`

**Files:**
- Modify: `app/config.py`
- Modify: `.env.example`

Add to `Settings`:

```python
openai_api_key: SecretStr = SecretStr("")
openai_model: str = "gpt-4o-mini"
fake_fail_rate: float = 0.0
fake_latency_ms: int = 0
fake_fail_mode: str = "timeout"
```

Use `pydantic.SecretStr` so the key is never printed in logs or tracebacks.

Update `.env.example` with placeholders only:

```bash
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini
FAKE_FAIL_RATE=0.0
FAKE_LATENCY_MS=0
FAKE_FAIL_MODE=timeout
```

**Exit criteria:** `Settings()` loads without exposing `openai_api_key` in `repr`.

---

## Task 7: Verification

```bash
make test
make lint
make typecheck
```

Expected: all green.

---

## Task 8: Commit and Backfill `TASKS.md`

Proposed task IDs:
- `DT-17`: extend `ChunkRequest` with context fields
- `DT-18`: implement `FakeProvider`
- `DT-19`: implement `ModelCostCalculator`
- `DT-20`: implement `OpenAIProvider` with structured outputs
- `DT-21`: port test-kit and settings wiring

Or combine into a single commit:

```bash
git add app/core/models.py tests/test_core_models.py app/adapters/llm app/config.py .env.example tests/adapters/llm TASKS.md
git commit -m "DT-17: feat(llm): implement LLM provider layer with fake and openai adapters"
```

---

## Critical Agent Reminders

1. **Tiktoken caching.** Do **not** call `tiktoken.encoding_for_model` inside `translate_chunk`. Initialize the encoder once at module or class level and reuse it.
2. **Structured Outputs.** Use OpenAI Structured Outputs (`client.beta.chat.completions.parse` or `response_format={"type": "json_schema"}`) with a Pydantic schema; do not rely on the model emitting free-form JSON.
3. **Missing block validation.** `OpenAIProvider` must check that every `block.id` from the requested chunk appears in the response translations. Missing blocks are a **retryable** error.
4. **Context blocks.** Include `context_before`/`context_after` in the prompt, but do not expect or validate translations for them.
5. **Secrets.** `OPENAI_API_KEY` must be a Pydantic `SecretStr`; never log or print it.
