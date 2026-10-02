# Stage 2 — Execution record

The user authorized implementation of the approved Stage 2 LLM provider plan,
including decomposition, delegation, review, verification, and commits.
The source-side context extension is already approved in the existing plan and
the Stage 2 corrections recorded in `PROMPTS.md`.

## Decomposition and ownership

- DT-17: the orchestrator owns context fields and roundtrip tests.
- DT-18: `fake_settings` owns FakeProvider, Settings, environment placeholders,
  and focused fake/config tests.
- DT-19: `pricing` owns the cost calculator and tests, then reviews the other
  changes independently without modifying them.
- DT-20: `openai` owns the real adapter, offline SDK tests, and opt-in live test.
- DT-21: the orchestrator owns the reusable provider port kit, integration,
  architecture documentation, full checks, delivery, and task-hash backfills.

Each agent has disjoint file ownership and is instructed to preserve other
agents' edits. `superpowers:executing-plans`, referenced by the draft, is not
installed; the approved steps use the available collaboration tools instead.

## Implementation corrections and review

Architecture references: “Layering & the Document IR”, “Pipeline”,
“LLM provider & the agent question”, “Failure handling matrix”, and
“Testing strategy”. No format adapter, queue, REST, DB, MCP, or triage contract
is added in this stage.

1. A strict response with arbitrary dictionary keys is incompatible with
   OpenAI's required `additionalProperties: false`. The internal wire schema
   instead contains a list of `{block_id, translated_text}` records; after
   exact-ID and duplicate validation, it converts into the unchanged public
   `ChunkResult.translations` mapping. Verified against the installed SDK
   source and the [official Structured Outputs guide](https://developers.openai.com/api/docs/guides/structured-outputs).
2. `core/errors.py` was absent. Safe catalogued `ProviderError` failures now
   carry retryability and known usage without raw provider messages. Timeout,
   connection, rate-limit, server, and invalid-response failures are retryable;
   request, authorization, and refusal failures are fatal.
3. SDK automatic retries are disabled so the future worker can account for
   every invocation. Missing token usage does not become a free successful
   result. Truncated or rejected responses retain known usage where the SDK
   exposes it. Transport failures have unknown spend, consistent with the
   architecture's at-least-once boundary.
4. The initial FakeProvider draft substituted regex token estimates when the
   encoding cache was absent. Review rejected that substitution: runtime uses
   a cached tiktoken encoder, initialized outside the event loop; offline tests
   use a local tiktoken fixture to avoid network downloads.
5. JSON escaping tests validate recovered data, including quotes and newlines,
   rather than requiring an incorrectly unescaped prompt substring. Context
   blocks are source-only, never part of the expected result IDs. Opaque
   metadata is neither inspected nor included in prompts.
6. Model prices are explicitly the approved plan's snapshot, not a claim of
   current provider rates. Unknown models and negative counts fail explicitly.

## Verification and delivery

Acceptance: `make test` — 205 passed and 1 live test deselected; `make lint` —
clean; `make typecheck` — clean, 20 source files. The two existing Pydantic
warnings for approved `register` fields remain. Threaded tests passed outside
the tool sandbox, which stalled `asyncio.to_thread`. Real API calls were not
run. Delivery commit hashes are recorded in `TASKS.md`. Calls are opt-in through
`make test-live`; ordinary
tests use FakeProvider or offline SDK substitutes and spend no API credits.
Pytest's default marker selection excludes live tests even when invoked directly;
an explicit `-m live` overrides that default. Runtime FakeProvider may download
tiktoken's encoding data on first use if its local cache is empty; normal tests
use a local encoding and do not require that download.

Existing user changes in `PROMPTS.md`, `TEST_TASK.md`, `docs/roadmap.md`, and the
approved Stage 2 plan are preserved outside the implementation commits.
Ruff required one formatting-only correction to an assignment inside the
approved plan's Python code fence; its implementation requirements are unchanged.
