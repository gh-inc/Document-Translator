# Triage agent efficiency and analysis-cost visibility — implementation plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Stop the triage stage from spending three provider attempts on failures
that cannot succeed on a retry, make a loop-exhausted agent fail fast instead of
re-running, turn the hardcoded agent limits into validated settings, and show the
document-analysis cost in the UI next to the bulk translation cost it precedes.

**Architecture:** Two independent backend changes, then two thin layers on top.
The service loop stops honouring `retryable=False`, matching what the bulk
translation path already does through `Executor`. Turn-budget exhaustion becomes
terminal *for triage only*, via a `ProviderError` subclass that leaves the
catalogued meaning of `PROVIDER_INVALID_RESPONSE` untouched. The dead
`MAX_TOOL_CALLS` cap is removed and the two real limits become validated
`Settings` fields, with the service-level guard derived from the adapter timeout so
the new knob cannot become a trap. Finally, the already-durable
`document_analyses.cost_usd_total` is surfaced on the existing document endpoint
and rendered once per batch.

**Tech Stack:** Python 3.12, FastAPI, pydantic-settings, openai-agents SDK,
structlog, React 19 + TypeScript + Vite, pytest, Vitest. No new dependency, no
schema change.

**Ticket:** DT-92 (stage 11 — defects found in operation).

---

## Origin: what the audit found

An audit of the triage stage against `openai-agents` guidance produced four
recommendations. Three are already satisfied or need no code; the fourth exposed a
real defect the audit did not list.

| Recommendation | Verdict |
|---|---|
| Agent used only for triage, strictly before chunking | **Already correct.** `JobService.create_jobs` reads `document_analyses`, builds `TranslationPlan`, and only then calls `_group_blocks` (`app/core/services/job_service.py:92-138`). The plan travels to the provider in `ChunkRequest.plan`. Recorded as a decision at `ARCHITECTURE.md:93`. |
| Bound the agent loop's maximum steps | **Present but not configurable.** `Runner.run(..., max_turns=self._max_turns)` with `DEFAULT_MAX_TURNS = 8` (`app/adapters/llm/triage_agent.py`). `create_triage_agent` constructs `OpenAITriageAgent(settings=settings)`, so the value is a constructor default and never a setting. |
| Compact tool outputs | **Already done well.** `MAX_READ_BLOCKS=8`, `MAX_SEARCH_RESULTS=8`, `MAX_SNIPPET_CHARS=1000`, `MAX_TOOL_OUTPUT_CHARS=16000`, and `_bounded_json` shrinks the result list until the *serialized* payload fits, so JSON escaping overhead is accounted for rather than assumed. |
| Cache the agent's prompt | **Already handled by the SDK, not by us.** `agents/run.py:818` builds a `PromptCacheKeyResolver`, which generates one `prompt_cache_key` per run for models that support it (`agents/run_internal/prompt_cache_key.py:18-30`). Cached input is already priced at the discounted rate (`app/adapters/llm/pricing.py:32-55`). No code change; the fact is currently undocumented. |
| Skip the agent for tiny documents | **Rejected — see Out of scope.** The expensive duplicate case is already free: byte-identical re-uploads resolve to the same `document_id` and never schedule triage at all. |

### Defect 1 — the retry loop ignores the error's own retryability

`TriageService.run` catches bare `Exception` and always retries three times,
while the bulk path's `Executor` checks `error.retryable`. `PROVIDER_AUTH_ERROR`,
`PROVIDER_BAD_REQUEST` and `PROVIDER_REFUSAL` are all `retryable=False` in
`app/core/errors.py:50-56`, so a misconfigured key or a rejected request costs
three round trips per document before the degraded fallback.

### Defect 2 — a loop-exhausted agent is retried three times

`MaxTurnsExceeded` is an `AgentsException`, caught by the adapter's broad clause
and mapped to `PROVIDER_INVALID_RESPONSE`, which the catalog marks
`retryable=True`. The service therefore runs **three complete agent runs** for a
document whose turn budget was exhausted — the most expensive possible failure
mode, and one that is unlikely to differ on the second attempt because the same
prompt and the same model produce the same loop.

Note that flipping `PROVIDER_INVALID_RESPONSE` to `retryable=False` in the
catalog is **not** the fix: that code is also produced by the bulk translation
path for a malformed provider response, where retrying is correct. The fix has to
mark this *occurrence* terminal without changing the code's shared meaning. That
is why Task 2 introduces `TriageTerminalError` instead of a catalog edit or a new
error code.

### Secondary findings

- `MAX_TOOL_CALLS = 16` is **unreachable**. With `parallel_tool_calls=False` and
  `tool_choice="required"` there is at most one tool call per turn, so
  `max_turns=8` binds first. A guardrail that can never fire is a reviewer
  question, not a protection.
- `TriageService.attempt_timeout_seconds` defaults to 65 while the adapter's own
  timeout is 60 (`triage_agent.py`, `DEFAULT_TIMEOUT_SECONDS`). That 5-second
  margin is deliberate — it lets the adapter raise `ProviderError` carrying its
  accumulated usage before the outer guard cancels it. Any new timeout setting
  must preserve this ordering.

### Owner-requested addition — analysis cost in the UI

The bulk translation cost is already displayed per job
(`frontend/src/features/jobs/JobCard.tsx:83`). The document analysis that
precedes it has no UI surface at all, even though its cost is durably recorded in
`document_analyses.cost_usd_total` (`cost_usd`, `tokens_in`, `tokens_out` and
their `_total` counterparts) and already exported globally as
`llm_triage_cost_usd_total`. The work is therefore a surfacing task, not an
instrumentation task: Tasks 4 and 5.

---

## Invariants this plan must not break

1. **Usage accounting is unchanged on every path.** A failed attempt may still
   have been billed; its usage must be appended to the durable cumulative totals
   exactly as today, including on the attempt that ends the loop early.
2. **Only a `ProviderError` can be terminal.** A bare `Exception` from an injected
   agent has no `retryable` attribute and keeps the existing three-attempt
   behavior. `tests/api/test_triage_background.py:243` asserts `calls == 3` with
   an agent that raises `RuntimeError`; that test must stay green **without being
   edited**.
3. **The degraded fallback still runs.** Ending the loop early must fall through
   to `degraded_plan(document_ir)`, publish `DocumentStatus.EXTRACTED`, and leave
   jobs creatable. Retry count is a cost decision, never a correctness one.
4. **The adapter timeout stays strictly below the service guard.** The 5-second
   margin exists so the adapter's own `ProviderError` (with usage) wins the race
   against the outer `asyncio.timeout`.
5. **No public error-code change.** `PROVIDER_INVALID_RESPONSE` keeps its
   catalogued message *and* its `retryable=True` value for every other caller.
   Only the triage loop treats `TriageTerminalError` as terminal, and that class
   never escapes the triage service — a failed analysis becomes a degraded plan,
   not an outward error.
6. **Settings are validated, not trusted.** `triage_max_turns` and
   `triage_timeout_seconds` get bounds in `Settings`, not bare assignments.
7. **The analysis cost shown is the cumulative one.** `cost_usd_total` includes
   every re-triage, so a document analyzed three times shows what it actually
   cost. `cost_usd` alone would silently under-report after a retry.
8. **No secret or document text in new log lines or responses.** Counters, codes
   and money only.

---

## Decisions taken (owner-approved)

- **Retryable failures stop the loop immediately**; bare exceptions keep three
  attempts.
- **Turn-budget exhaustion is terminal for triage**, implemented as a
  `ProviderError` subclass rather than a catalog change.
- **Prompt caching needs no code** — document that the SDK supplies the key.
- **The analysis cost is rendered once per batch, not once per job.** See Task 5
  for why.
- **No fast path for small documents.** See Out of scope.

### Rejected alternatives

1. **Flip `PROVIDER_INVALID_RESPONSE` to `retryable=False` in the catalog.**
   Rejected: the bulk translation path maps a malformed provider response to that
   same code and is right to retry it. One flag cannot mean both things.
2. **Add a `provider_turn_limit` error code to the catalog.** Rejected as
   unnecessary public surface: triage failures never reach a client, so a new
   outward code would describe something no client can observe.
3. **Retry only when `error.retryable is True` for every exception.** Rejected: a
   bare exception carries no such attribute; treating "unknown" as terminal would
   silently change behavior for injected agents and break an existing test.
4. **Lower `MAX_TOOL_CALLS` to 8 instead of removing it.** Rejected: a second cap
   duplicating `max_turns` invites the same "which one binds?" question. One cap,
   documented.
5. **Expose a `triage_tool_call_budget` setting.** Rejected: unreachable by
   construction, so a setting for it would be a lie.
6. **Derive the service guard from the adapter timeout inside the adapter.**
   Rejected: the adapter does not know the service exists. The wiring belongs in
   `triage_runtime.prepare_triage`, which constructs both.
7. **Put the analysis cost on every job payload.** Rejected: `list_recent_jobs`
   would issue one extra query per job and the one-second SSE poll would double
   its query count, to repeat a document-level number N times.
8. **Show the analysis cost on the upload page.** Rejected: `UploadPage`
   navigates to the batch as soon as submission completes, so the number would be
   on screen for a fraction of a second.

---

## Task 1: Honor `retryable` in the triage attempt loop

**Files:**
- Modify: `app/core/services/triage_service.py` (the `for attempt in range(3)`
  loop inside `TriageService.run`)
- Test: `tests/api/test_triage_background.py`

**Step 1 — write the failing test**

```python
async def test_non_retryable_failure_stops_after_one_attempt(runtime) -> None:
    app, settings, client = runtime
    agent = ControlledAgent(error=ProviderError(ErrorCode.PROVIDER_AUTH_ERROR, model="gpt-4o-mini"))
    app.state.triage_agent_factory = lambda _settings: agent
    document_id = (await upload(client)).json()["id"]

    assert agent.calls == 1
    async with repository(settings) as repo:
        assert (await repo.get_document(document_id)).status is DocumentStatus.EXTRACTED
        assert (await repo.get_analysis(document_id)).triage_status is TriageStatus.DEGRADED
```

and a second test asserting the failed attempt is still billed — construct the
agent with `tokens_in=…, tokens_out=…` on the error and assert the cumulative
`tokens_in_total` on the published analysis is non-zero (invariant 1).

Extend `ControlledAgent` with an optional `error` parameter. Keep `fail=True`
raising the bare `RuntimeError` so the existing three-attempt test is untouched
(invariant 2).

**Step 2 — run and watch it fail**

`uv run pytest tests/api/test_triage_background.py -q -k non_retryable`
Expected: `assert 1 == 3`.

**Step 3 — implement**

In the loop's `except` clause, after the existing usage bookkeeping, stop when the
error is terminal:

```python
            except Exception as error:
                if not provider_returned:
                    attempts.append(_usage_from_error(error))
                terminal = isinstance(error, ProviderError) and error.terminal
                logger.warning(
                    "triage_attempt_failed",
                    document_id=document_id,
                    attempt=attempt + 1,
                    terminal=terminal,
                )
                if terminal or attempt >= 2:
                    break
                await asyncio.sleep(self._retry_delay_seconds * (2**attempt))
```

The usage append stays **before** the break (invariant 1). `plan` is still
`None`, so control falls through to `degraded_plan(document_ir)` and the existing
publication path runs unchanged (invariant 3).

**Step 4 — declare `terminal` on `ProviderError`**

In `app/core/errors.py`, add a class attribute next to the existing fields:

```python
class ProviderError(Exception):
    ...
    #: This occurrence cannot succeed on retry even though its catalogued code
    #: may be retryable in general (see TriageTerminalError).
    terminal: bool = False
```

`error.terminal` is `False` for every existing raise site, so no other behavior
moves. `Executor` in the bulk path is untouched: it still keys off `retryable`.

**Step 5 — run**

`uv run pytest tests/api/test_triage_background.py tests/services -q`
Expected: all pass, including `test_three_failures_publish_degraded_plan_and_allow_jobs`
unmodified.

**Commit only when the owner asks:**
`DT-92: fix(triage): stop retrying terminal provider failures`

---

## Task 2: A loop-exhausted agent must not be retried

**Files:**
- Modify: `app/core/errors.py` (add `TriageTerminalError`)
- Modify: `app/adapters/llm/triage_agent.py` (catch `MaxTurnsExceeded` first)
- Test: `tests/adapters/` — the nearest existing triage-agent test module. Check
  with `ls tests/adapters` before creating a file.

**Step 1 — write the failing test**

```python
async def test_turn_budget_exhaustion_is_terminal_and_logged(caplog) -> None:
    agent = OpenAITriageAgent(settings=settings, client=LoopingStubClient(), max_turns=1)
    with pytest.raises(TriageTerminalError) as raised:
        await agent.analyze(document_ir)
    assert raised.value.error_code is ErrorCode.PROVIDER_INVALID_RESPONSE
    assert raised.value.retryable is True  # the catalog is unchanged
    assert raised.value.terminal is True
    assert "triage_turn_budget_exhausted" in caplog.text
```

Both assertions matter: `retryable is True` pins invariant 5, and `terminal is
True` is the behavior the triage loop keys off.

**Step 2 — run and watch it fail**

Expected: `TriageTerminalError` is not defined, and the raised error is a plain
`ProviderError` with `terminal` absent.

**Step 3 — add the subclass** (`app/core/errors.py`)

```python
class TriageTerminalError(ProviderError):
    """A provider failure that must not be retried for the same document.

    ``retryable`` from the catalog keeps describing the *code*; this class marks
    the *occurrence* as terminal. Turn-budget exhaustion is used here because the
    same prompt and model reproduce the same loop, so a retry buys three runs of
    the same failure. The bulk translation path never raises this class, so the
    shared meaning of PROVIDER_INVALID_RESPONSE is untouched.
    """

    terminal: bool = True
```

**Step 4 — raise it from the adapter** (`triage_agent.py`)

This module has **no logger today** — it never logs, it only raises. Add one,
matching `app/worker/translation_loop.py`:

```python
import structlog

logger = structlog.get_logger(__name__)
```

Then import `MaxTurnsExceeded` from `agents.exceptions` — read the module first to
confirm the export path, do not guess it. Add a clause **above** the existing
broad `except (openai.OpenAIError, AgentsException, …)`:

```python
        except MaxTurnsExceeded:
            logger.warning(
                "triage_turn_budget_exhausted",
                model=model,
                max_turns=self._max_turns,
            )
            raise TriageTerminalError(
                ErrorCode.PROVIDER_INVALID_RESPONSE,
                **_usage_values(context_wrapper.usage),
                model=model,
            ) from None
```

`_usage_values` already returns exactly the four keyword names `ProviderError`
accepts (`tokens_in`, `tokens_out`, `cached_tokens_in`, `requests`), so the
unpacking is correct as written — keep the failed run's usage on the error so the
loop still records what it spent (invariant 1).

Keep the catalogued code and therefore `retryable=True` (invariant 5); the
`terminal` flag is what Task 1's loop reads. The broad clause still catches it as
a fallback, so this branch changes classification and logging only.

**Step 5 — run**

`uv run pytest tests/adapters tests/api/test_triage_background.py -q`
Expected: all pass. Add one integration case to Task 1's file: an agent whose
`analyze` raises `TriageTerminalError` must be called exactly once.

**Commit only when the owner asks:**
`DT-92: fix(triage): fail fast when the agent exhausts its turn budget`

---

## Task 3: Replace the dead cap with configured limits

**Files:**
- Modify: `app/adapters/llm/triage_agent.py` (`MAX_TOOL_CALLS`, `_NavigationBudget`)
- Modify: `app/adapters/llm/triage_runtime.py` (`create_triage_agent`, `prepare_triage`)
- Modify: `app/config.py`
- Modify: `docker-compose.yml` (optional passthrough — shared file)
- Test: `tests/test_config.py`, `tests/adapters/`

**Step 1 — write the failing tests**

```python
def test_triage_limits_are_validated() -> None:
    with pytest.raises(ValidationError):
        Settings(triage_max_turns=0)
    with pytest.raises(ValidationError):
        Settings(triage_timeout_seconds=0.0)
    with pytest.raises(ValidationError):
        Settings(triage_max_turns=999)


def test_agent_factory_applies_configured_limits() -> None:
    agent = create_triage_agent(Settings(triage_max_turns=3, triage_timeout_seconds=12.0))
    assert agent._max_turns == 3
    assert agent._timeout_seconds == 12.0
```

If reading private attributes is unacceptable in this repo, assert instead that
`analyze` fails after three turns with a stub client — say which approach was
taken in the execution record.

**Step 2 — run and watch them fail**

`uv run pytest tests/test_config.py -q -k triage`
Expected: unexpected keyword argument / missing validation.

**Step 3 — add the settings**

```python
triage_max_turns: int = Field(default=8, ge=1, le=20)
triage_timeout_seconds: float = Field(default=60.0, gt=0.0, le=300.0, allow_inf_nan=False)
```

The upper bound of 20 exists so a mistyped value cannot produce a run that bills
for hours. `300.0` stays above `MCP_TRIAGE_TIMEOUT_SECONDS` (default 45); note in
the docstring that raising it does not shorten MCP waits, because an MCP client
stops polling at its own timeout while the background triage continues.

**Step 4 — remove the dead cap**

In `triage_agent.py`, delete `MAX_TOOL_CALLS` and `consume()`. Keep
`_NavigationBudget` for `successful_reads`, which is what actually enforces "the
agent must have read something before its plan is accepted":

```python
@dataclass
class _NavigationBudget:
    successful_reads: int = 0
```

Update the two tool wrappers to stop calling `budget.consume()`. Add a comment on
`max_turns` stating that it is the single cap on tool calls, since
`parallel_tool_calls=False` allows at most one call per turn.

**Step 5 — wire the settings**

`triage_runtime.create_triage_agent`:

```python
    return OpenAITriageAgent(
        settings=settings,
        max_turns=settings.triage_max_turns,
        timeout_seconds=settings.triage_timeout_seconds,
    )
```

`triage_runtime.prepare_triage`, where `TriageService` is constructed — derive the
outer guard so invariant 4 holds for any configured value:

```python
_ATTEMPT_GUARD_MARGIN = 5.0  # the adapter must raise with its usage first
...
        attempt_timeout_seconds=settings.triage_timeout_seconds + _ATTEMPT_GUARD_MARGIN,
```

**Step 6 — optional compose passthrough**

Add `TRIAGE_MAX_TURNS: ${TRIAGE_MAX_TURNS:-8}` and
`TRIAGE_TIMEOUT_SECONDS: ${TRIAGE_TIMEOUT_SECONDS:-60}` to the shared
`&app_environment` block. That file is shared with other owners' work — stage it
alone.

**Step 7 — run**

`uv run pytest tests/test_config.py tests/adapters -q` → Expected: all pass.

**Commit only when the owner asks:**
`DT-92: refactor(triage): make agent limits configured and remove the dead cap`

---

## Task 4: Expose the document analysis cost over REST

This adds one field to one response model — a **public contract addition**, so per
AGENTS.md rule 4 it needs the owner's explicit approval before implementation.
The plan documents it; do not write the code before the owner says yes.

**Files:**
- Modify: `app/api/schemas.py:8-15` (`DocumentUploadResponse`)
- Modify: `app/core/services/document_service.py` (`get_document`)
- Modify: `app/api/routers/documents.py:20-33` and the two other response sites
  (upload at 36-59, retry-triage at 62)
- Test: `tests/api/test_documents.py`, `tests/services/test_document_service.py`

**Step 1 — write the failing test**

```python
async def test_document_payload_reports_cumulative_analysis_cost(
    runtime: tuple[Settings, httpx.AsyncClient],
) -> None:
    settings, client = runtime
    connection = await SqliteConnectionFactory(settings.database_path).create()
    try:
        repo = SqliteDocumentRepository(connection)
        async with transaction(connection):
            await repo.create_document("report", "report.docx", "docx", 20, "/private/report")
            await repo.create_blocks(
                "report",
                [
                    Block(id="a", seq=0, source_text="First", source_hash="first"),
                    Block(id="b", seq=1, source_text="Second", source_hash="second"),
                ],
            )
            await repo.save_analysis(
                "report",
                TranslationPlan(source_language="en", domain="general", register="neutral"),
                cost_usd=0.0011,
                cost_usd_total=0.0034,
            )
        response = await client.get("/api/documents/report")
        assert response.status_code == 200
        assert response.json()["analysis_cost_usd"] == 0.0034
    finally:
        await connection.close()
```

Assert `cost_usd_total`, not `cost_usd` (invariant 7): with `cost_usd=0.0011`
and `cost_usd_total=0.0034` the two are distinguishable, so a later regression to
the wrong column fails. Add a second test that a document with **no** analysis row
reports exactly `0.0`, and a third asserting the upload response is `0.0` because
analysis has not run when the response is sent — that documents the timing rather
than leaving it to inference.

Follow the existing fixture style in `tests/api/test_documents.py`: its `runtime`
fixture yields `(settings, client)` and there is **no** `upload()` helper in this
module, unlike `tests/api/test_triage_background.py`, which has one.

**Step 2 — run and watch it fail**

`uv run pytest tests/api/test_documents.py -q -k cost` → Expected: `KeyError`.

**Step 3 — extend the service**

`DocumentService.get_document` has exactly one caller (the GET route), so widen
its return:

```python
    async def get_document(
        self, document_id: str
    ) -> tuple[DocumentRecord, int, DocumentAnalysisRecord | None]:
        document = await self._document_repo.get_document(document_id)
        if document is None:
            raise ServiceError(ErrorCode.NOT_FOUND, status_code=404)
        blocks = await self._document_repo.get_blocks(document_id)
        analysis = await self._document_repo.get_analysis(document_id)
        return document, len(blocks), analysis
```

`DocumentRepository.get_analysis` already exists and is already used by
`JobService`, so no new query surface is introduced beyond the one extra read on
this route.

**Step 4 — add the field**

```python
class DocumentUploadResponse(BaseModel):
    id: str
    filename: str
    format: str
    status: DocumentStatus
    block_count: int
    analysis_cost_usd: float = 0.0
    warnings: list[str] = Field(default_factory=list)
```

Populate it in all three response sites with
`analysis.cost_usd_total if analysis is not None else 0.0`. On the upload
response it is always `0.0`, because analysis has not run when the response is
sent — a test asserting that literal documents the timing rather than leaving it
to inference.

**Step 5 — run**

`uv run pytest tests/api tests/services -q` → Expected: all pass.

**Commit only when the owner asks:**
`DT-92: feat(api): report cumulative document analysis cost`

---

## Task 5: Show the analysis cost in the UI

**Files:**
- Modify: `frontend/src/api/types.ts` (`DocumentUploadResponse`)
- Modify: `frontend/src/api/client.ts:13-16` (`parseDocument`)
- Modify: `frontend/src/features/jobs/BatchPage.tsx`
- Create: `frontend/src/features/jobs/BatchPage.test.tsx`
- Test: `frontend/src/api/client.test.ts`

**Step 1 — write the failing tests**

`client.test.ts` — extend the malformed-payload matrix with
`{ ...document, analysis_cost_usd: '0.01' }` and
`{ ...document, analysis_cost_usd: -1 }`; both must be rejected. `parseDocument`
uses the existing `money()` guard, which already enforces a finite non-negative
number.

`BatchPage.test.tsx` — new file; there is none today:

```tsx
  it('shows the document analysis cost once for the batch', async () => {
    vi.mocked(api.getBatch).mockResolvedValue(batchOfTwoJobs);
    vi.mocked(api.getDocument).mockResolvedValue({ ...document, analysis_cost_usd: 0.0042 });
    render(<MemoryRouter><Routes><Route path="/batches/:batchId" element={<BatchPage />} /></Routes></MemoryRouter>);
    expect(await screen.findByText('Document analysis: $0.0042')).toBeInTheDocument();
    expect(screen.getAllByText(/^Cost:/)).toHaveLength(2);
  });

  it('still renders the batch when the document request fails', async () => {
    vi.mocked(api.getDocument).mockRejectedValue(new Error('offline'));
    // …the job cards must still appear, with no analysis line
  });
```

The second test pins the failure mode: a missing cost must never break the batch
view.

**Step 2 — run and watch them fail**

`npm --prefix frontend test -- BatchPage` → Expected: fail.

**Step 3 — types and validation**

Add `analysis_cost_usd: number;` to `DocumentUploadResponse` and extend
`parseDocument` with `|| !money(value.analysis_cost_usd)`, returning the field.
`api.getDocument` already exists, so no new client function is needed.

**Step 4 — render**

`BatchPage` already receives `batch.jobs[0].document_id`. Fetch the document once
per batch — **not** once per job (rejected alternative 7) — and render one line
above the job grid:

Note for the implementer: `api.getDocument` is already called from `JobCard`, but
only inside the download handler to infer a file extension when the blob's MIME
type is ambiguous (`JobCard.tsx:56`). It is not fetched on render, so the batch
page's call is genuinely new and not a duplicate of existing traffic.

```tsx
{analysis && (
  <p className="mb-4 text-neutral-300">
    Document analysis: <span>${analysis.analysis_cost_usd.toFixed(4)}</span>
  </p>
)}
```

Guard the fetch with its own try/catch that leaves `analysis` undefined on
failure, so the batch renders regardless. Keep the label exactly "Document
analysis" and the money format identical to `JobCard.tsx:83` (`toFixed(4)`), so
the two figures read as one scale.

**Step 5 — run**

`npm --prefix frontend test` and `npm --prefix frontend run build` — the Docker
image builds the frontend, so a type error breaks the image build.

**Commit only when the owner asks:**
`DT-92: feat(frontend): show document analysis cost per batch`

---

## Task 6: Documentation

**Files:**
- Modify: `DECISIONS.md` — new numbered record
- Modify: `ARCHITECTURE.md` — *LLM provider & the agent question* ("Where the
  agent earns its keep", 496-515), and *Observability* if the new field changes
  any stated metric

**Step 1 — record the SDK's prompt caching**

State that cache affinity is supplied by `openai-agents` (a generated
`prompt_cache_key` per run), that cached input is priced at the discounted rate,
and that `prompt_cache_retention` is deliberately unset because a triage run lasts
seconds. Without this, a future reader will reasonably assume prompt caching was
overlooked.

**Step 2 — record the fail-fast boundaries explicitly**

- Non-retryable provider errors end the attempt loop after one attempt.
- Turn-budget exhaustion is terminal *for triage only*, via `TriageTerminalError`;
  `PROVIDER_INVALID_RESPONSE` keeps its catalogued `retryable=True` because the
  bulk path must keep retrying a malformed response.
- A bare `Exception` from an injected agent keeps three attempts.
- The failed attempt is still billed and still recorded in the cumulative totals.
- The adapter timeout stays 5 seconds below the service guard so the adapter's
  error, carrying usage, wins the race against cancellation.

**Step 3 — document the new knobs and the new field**

`max_turns` is the single cap on tool calls; both limits are settings. The
document payload's `analysis_cost_usd` is cumulative across re-triages, and is
rendered once per batch because analysis is per document while translation is per
job.

**Commit only when the owner asks:**
`DT-92: docs(triage): record fail-fast policy, limits and analysis cost`

---

## Task 7: Delivery

**Step 1 — full suites**

`make test` · `make lint` · `make typecheck` · `npm --prefix frontend test` ·
`npm --prefix frontend run build`. No `@pytest.mark.live` test is added; the
provider boundary is untouched.

**Step 2 — record the observable effect**

Two claims are worth making concrete rather than asserting:

- With a deliberately invalid `OPENAI_API_KEY`, one upload should now produce
  **one** `triage_attempt_failed` line instead of three, and reach the degraded
  plan in a third of the wall time. `LLM_PROVIDER=fake` cannot show this, because
  the fake agent fails on demand rather than on credentials.
- With `LLM_PROVIDER=fake`, the analysis cost is still non-zero —
  `FakeTriageAgent` reports 173/29 tokens with 61 cached (`fake_triage_agent.py`),
  priced by `ModelCostCalculator` — so the new UI figure can be demoed with no
  provider spend at all.

Record the observed log lines in the execution record. If no invalid key is
available, say the claim rests on the unit test alone.

**Step 3 — `TASKS.md`**

Add
`| DT-92 | 11 | Fail fast on terminal triage failures, configure agent limits, surface analysis cost | done | <hash> |`
plus a stage-11 execution paragraph.

**Step 4 — leave it uncommitted**

The owner has not asked for a commit. Report the diff and wait.

---

## Verification matrix

| Requirement | Test | Status |
|---|---|---|
| Non-retryable failure stops after one attempt | `tests/api/test_triage_background.py` | new |
| The early-stopping attempt is still billed and recorded | same | new |
| Turn-budget exhaustion is terminal, catalog unchanged | `tests/adapters/` | new |
| Turn-budget exhaustion is logged once | same | new |
| `TriageTerminalError` stops the loop in an integration run | `tests/api/test_triage_background.py` | new |
| Retryable failure still gets three attempts | `test_three_failures_publish_degraded_plan_and_allow_jobs` | existing, unmodified |
| Bare `Exception` keeps three attempts | same | existing |
| Degraded fallback still publishes and allows jobs | same | existing |
| `Executor` in the bulk path is unaffected | whole suite | existing |
| Settings bounds reject `0`, `999`, `0.0` | `tests/test_config.py` | new |
| Factory applies configured limits | same | new |
| Service guard stays above the adapter timeout | `tests/adapters/` | new |
| No unreachable cap remains | grep in Task 3 review | manual |
| Document payload reports cumulative analysis cost | `tests/api/test_documents.py` | new |
| Upload response reports `0.0` before analysis runs | same | new |
| Client rejects malformed cost payloads | `frontend/src/api/client.test.ts` | new |
| Batch renders the analysis cost once | `frontend/src/features/jobs/BatchPage.test.tsx` | new |
| Batch survives a failed document request | same | new |

## Out of scope

- **A fast path that skips the agent for small documents.** The only generator
  available is `degraded_plan()`, and `triage_status=DEGRADED` currently *means*
  "analysis failed": `discard_degraded_analysis`, the `ANALYZING→EXTRACTED`
  repair in `TriageService.claim`, and `POST /api/documents/{id}/retry-triage` all
  key off it. A fast path would make "degraded" mean both "failed" and "skipped".
  Doing it properly needs a distinct status — a schema and contract change, not an
  optimization.
- **Changing any catalogued error's `retryable` value.** `TriageTerminalError`
  exists precisely so this is unnecessary.
- **An aggregate tool-output budget across a whole run.** Only per-call and
  per-turn bounds exist today; a worst-case run is roughly 8 × 16k characters.
  Worth measuring before adding a third knob.
- **Caching triage plans across near-identical documents.** Documents are
  content-addressed, so only genuinely different documents are triaged, but two
  documents differing by one paragraph still pay for a full triage.
- **A token-count or latency breakdown in the UI.** The request was cost; tokens
  are already durable and exported, and a second number on the same screen would
  compete with the cost figure rather than explain it.
- **MCP exposure of the analysis cost.** `translate_file` and `check_status`
  report job cost only. Adding a document-level figure to MCP is a separate
  contract decision.
- **Multi-agent triage, embedding-based tool search, or `tool_choice` relaxation.**
- **Anything about the bulk translation path.**

## Execution notes

- **Never commit without an explicit request.** Every commit block is gated on
  the owner asking.
- **Task 4 changes a public response model.** It is a contract addition and needs
  an explicit yes before implementation, like Task 8 of DT-91.
- Three files are shared with other owners' in-flight work: `docker-compose.yml`,
  `tests/api/test_triage_background.py` and `frontend/src/api/client.ts`. Edit
  them surgically, stage by explicit path, do not reformat neighbouring lines.
- If implementing Task 3's factory test requires changing the `TriageAgent` port
  to expose limits, stop — the port should stay as it is; the limits belong to the
  OpenAI adapter.
- Before importing `MaxTurnsExceeded`, read `agents/exceptions.py` in `.venv` to
  confirm the export path.
- The `5.0` guard margin and the `le=20` turn ceiling are judgement calls: the
  margin preserves an existing deliberate ordering, and the ceiling exists so a
  typo cannot become a multi-hour billed run. Both are stated in the settings
  docstrings.
- If Task 4 is declined, Tasks 5 must be dropped too — a UI cost with no field to
  read is dead code.