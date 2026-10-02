# Stage 6 — Triage Agent (Async, Non-Blocking)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the synchronous triage stub with a real OpenAI Agents SDK triage stage that runs as a background task after upload, investigates large documents through navigation tools, and recovers from stuck states.

**Architecture:**
- **Agent earns its keep through navigation.** The triage agent does not use trivial wrappers like `detect_language` or `classify_domain`. Instead it receives document-navigation tools (`read_blocks`, `search_blocks`) and decides for itself which parts of a long document to inspect.
- **Chain-of-thought structured output.** The agent emits `TriageAgentOutput` containing `reasoning` (free-text chain-of-thought) and `plan` (`TranslationPlan`). The reasoning field forces the model to think before emitting the final structured plan, reducing hallucinations.
- **Background task.** `POST /api/documents` extracts blocks synchronously, sets status to `analyzing`, and schedules triage via FastAPI `BackgroundTasks`. The HTTP response returns immediately.
- **Stuck-state recovery.** FastAPI background tasks live in process memory; a `kill -9` during triage can leave a document stuck in `analyzing`. A `POST /api/documents/{id}/retry-triage` endpoint allows the client to re-trigger analysis.
- **Degraded fallback.** If the agent fails after retries, the background task writes a heuristic analysis (`triage_status=degraded`) and moves the document to `extracted`.
- **Background task owns its DB connection.** `run_triage` opens a fresh SQLite connection through `SqliteConnectionFactory`; it cannot reuse the request-scoped connection.

**Tech Stack:** Python 3.12, FastAPI, `openai-agents`.

**Current State:**
- `POST /api/documents` (Stage 5 stub) creates a default `DocumentAnalysisRecord` synchronously.
- No `TriageAgent` implementation exists.
- `DocumentStatus` does not yet have an `analyzing` state.

---

## Task 1: Extend `DocumentStatus` with `ANALYZING`

**Files:**
- Modify: `app/core/models.py`
- Modify: `tests/test_core_models.py`

Add:

```python
class DocumentStatus(StrEnum):
    UPLOADED = "uploaded"
    ANALYZING = "analyzing"
    EXTRACTED = "extracted"
    FAILED = "failed"
```

Update fixtures/tests that enumerate statuses.

**Exit criteria:** Tests pass; `DocumentStatus.ANALYZING` roundtrips JSON.

---

## Task 2: Define `TriageAgentOutput` Schema

**Files:**
- Modify: `app/core/models.py`
- Modify: `tests/test_core_models.py`

Add:

```python
class TriageAgentOutput(BaseModel):
    """Structured output from the triage agent."""

    model_config = ConfigDict(extra="forbid")

    reasoning: str
    plan: TranslationPlan
```

**Exit criteria:** `TriageAgentOutput` validates, serializes, and roundtrips.

---

## Task 3: Implement Navigation Tools and OpenAI Agent

**Files:**
- Create: `app/adapters/llm/triage_agent.py`

Tools (all receive `DocumentIR` through the agent run context):

```python
@function_tool
async def read_blocks(
    start_seq: int,
    count: int,
    context: RunContextWrapper[DocumentIR],
) -> str: ...


@function_tool
async def search_blocks(
    keyword: str,
    context: RunContextWrapper[DocumentIR],
) -> list[dict[str, object]]: ...
```

- `read_blocks` returns concatenated `source_text` of blocks with `seq` in `[start_seq, start_seq + count)`.
- `search_blocks` returns blocks whose `source_text` contains the keyword (case-insensitive), with limited metadata (`seq`, `source_text` snippet).

Agent:

```python
agent = Agent[DocumentIR](
    name="DocumentTriageAgent",
    instructions=(
        "You are a document triage analyst. Investigate the uploaded document "
        "using the navigation tools. Read the beginning, end, and any sections "
        "that look like a glossary or summary. Produce a TranslationPlan with "
        "source language, domain, register, terminology, and warnings."
    ),
    tools=[read_blocks, search_blocks],
    output_type=TriageAgentOutput,
)
```

Run:

```python
result = await Runner.run(agent, input="Analyze this document", context=document)
output: TriageAgentOutput = result.final_output
```

Implementation `OpenAITriageAgent(TriageAgent)` converts `output.plan` to a `DocumentAnalysisRecord` and returns it.

**Exit criteria:** Live test behind `@pytest.mark.live` produces a valid `TriageAgentOutput`.

---

## Task 4: Implement `FakeTriageAgent`

**Files:**
- Create: `app/adapters/llm/fake_triage_agent.py` (or include in `triage_agent.py`)

Behavior:
- Deterministic output based on document content.
- `source_language`: "en" or first non-English heuristic (fixed for tests).
- `domain`: "general".
- `register`: "neutral".
- `terms`: first N capitalized words from the document or a fixed list.
- `warnings`: empty.
- `triage_status`: `TriageStatus.OK`.
- Configurable failure mode for degraded-path tests.

**Exit criteria:** Unit tests prove deterministic output and failure injection.

---

## Task 5: Update `POST /api/documents` to Schedule Triage in Background

**Files:**
- Modify: `app/api/routers/documents.py`
- Create or modify: `app/api/background.py`

New flow:
1. Validate and save upload.
2. Extract blocks.
3. Create document record with `status=DocumentStatus.ANALYZING`.
4. Schedule background task:
   ```python
   background_tasks.add_task(run_triage, document_id)
   ```
5. Return `DocumentUploadResponse` with `status=analyzing`.

`run_triage(document_id)`:
- Create its own DB connection via `SqliteConnectionFactory`.
- If a non-degraded analysis already exists, skip.
- Load document and blocks.
- Instantiate `TriageAgent` (real or fake depending on config).
- Run agent with retries (up to 3).
- On success: save `DocumentAnalysisRecord` and set `status=extracted`.
- On failure after retries: save heuristic degraded analysis and set `status=extracted`.
- On extraction-level failure: set `status=failed` with error code.

**Exit criteria:** Upload returns immediately; analysis appears in DB shortly after.

---

## Task 6: Add `POST /api/documents/{id}/retry-triage`

**Files:**
- Modify: `app/api/routers/documents.py`

Behavior:
- Load document.
- If status is `analyzing` and the analysis lease/update time is stale, or if the user explicitly retries:
  - Reset `document_analyses` row (delete or mark for overwrite).
  - Schedule `run_triage(document_id)` as a background task.
  - Set `status=analyzing`.
- Return `DocumentUploadResponse` with current status.

**Exit criteria:** Integration test proves a stuck `analyzing` document can be retried and eventually reaches `extracted`.

---

## Task 7: Update `POST /api/jobs` to Check Analysis Readiness

**Files:**
- Modify: `app/api/routers/jobs.py`

Before `JobService.create_jobs`:
- Load document status and analysis.
- If `status != DocumentStatus.EXTRACTED` or no analysis exists, return 409 with `error_code="analysis_pending"`.

**Exit criteria:** Integration test asserts 409 during `analyzing` and success after analysis completes.

---

## Task 8: Tests

**Files:**
- Create: `tests/adapters/llm/test_triage_agent.py`
- Create: `tests/api/test_triage_background.py`

Tests:
1. **Fake agent smoke.** `FakeTriageAgent.analyze` returns a valid `TriageAgentOutput`.
2. **Navigation tools.** `read_blocks` and `search_blocks` return expected snippets.
3. **Non-blocking upload.** `POST /api/documents` returns 200 with `status=analyzing` before triage finishes.
4. **Analysis completes.** Poll until status is `extracted`; analysis exists.
5. **Degraded fallback.** Inject failure into `FakeTriageAgent`; assert document status becomes `extracted` with `triage_status=degraded`.
6. **Retry triage.** Force a document to `analyzing` without a running task; call `POST /api/documents/{id}/retry-triage`; assert it reaches `extracted`.
7. **No re-triage on re-upload.** Upload same file again; assert the original analysis row is reused.
8. **Live triage.** Behind `@pytest.mark.live`, run `OpenAITriageAgent` on a sample document.

**Exit criteria:** All tests pass.

---

## Task 9: Verification

```bash
make test
make lint
make typecheck
```

Expected: all green.

---

## Task 10: Commit and Backfill `TASKS.md`

Proposed task IDs:
- `DT-51`: extend `DocumentStatus` and add `TriageAgentOutput`
- `DT-52`: implement `FakeTriageAgent` and navigation tools
- `DT-53`: implement `OpenAITriageAgent`
- `DT-54`: wire background triage and retry endpoint
- `DT-55`: triage tests and live verification

Single-commit option:

```bash
git add app/core/models.py app/adapters/llm app/api/routers tests TASKS.md
git commit -m "DT-51: feat(triage): implement async researcher agent with retry endpoint"
```

---

## Critical Agent Reminders

1. **No trivial tools.** The agent gets only `read_blocks` and `search_blocks`. It derives language, domain, and terminology from the text itself.
2. **Chain-of-thought output.** Use `TriageAgentOutput` with `reasoning` + `plan` to improve structured-output quality.
3. **Background task owns its DB connection.** `run_triage` must open a fresh SQLite connection; it cannot use the request-scoped connection.
4. **Stuck-state recovery.** Implement `POST /api/documents/{id}/retry-triage` so a document stuck in `analyzing` after a crash can be recovered.
5. **Degraded fallback.** Triage failure must never block the pipeline; write a heuristic degraded plan and move to `extracted`.
6. **No re-triage.** If a non-degraded analysis already exists for a document, skip triage on re-upload.


## Execution rulings and ownership (2026-10-02)

The user's request to execute and commit this plan approves its proposed
public contracts. Repository tickets are DT-36–DT-40 (the provisional
DT-51–DT-55 numbers are not allocated).

- Orchestrator: core output/status, document/job/triage services, internal
  persistence, FastAPI composition, integration tests, docs and delivery.
- Adapter agent: OpenAI/fake triage, navigation tools, offline SDK tests and
  an opt-in live test. Installed SDK source inspected before implementation.
- Independent reviewer: final contract, layering, SDK and concurrency audit.

Rulings against the plan's internal inconsistencies:

| Tasks | Interface/requirement | Resolution |
|---|---|---|
| 1 / 5 / 6 / 7 | status lifecycle | analyzing after extraction; extracted only with analysis |
| 2 / 3 | reasoning output | brief evidence-based explanation; no private chain-of-thought prompt |
| 3 / 4 / 8 | analyze return type | preserve existing TriageAgent -> TranslationPlan port; service saves record |
| 3 / 8 | SDK context and output limits | context first per installed SDK; bounded JSON text snippets |
| 5 / 6 | stale timestamp absent from schema | explicit retry; process-local serialization, no schema migration |
| 5 / 6 / 8 | successful/degraded persistence | skip success; delete only degraded row in publication transaction |
| 5 / 8 | upload identity | SHA-256 bytes identity, shared upload lock; single web process |
| 7 / 8 | thin router and readiness | checks in core service and atomic enqueue persistence validation |
| 8 | non-blocking proof | observe ASGI response body while background agent is gated |
| 9 / 10 | verification and commits | required make checks; one implementation commit plus hash backfill |

Scope limits: no new document GET/polling endpoint or lease table; old UUID
uploads remain valid but are not retroactively deduplicated. Analysis retries
are explicit after process death. New implementation is documented against
ARCHITECTURE.md “LLM provider & the agent question” and “REST API surface”.
Unrelated user changes in DECISIONS.md, PROMPTS.md, TEST_TASK.md and
`docs/roadmap.md` are preserved and excluded from this delivery.


Independent-review correction: analysis is frozen after any translation job
exists. Retrying degraded/missing analysis then returns catalogued `409 conflict`;
otherwise changing source language/domain/register could reuse translations
cached under the earlier plan or change a resumed job mid-flight. The guard and
job readiness validation both execute under SQLite write transactions. This
preserves the approved cache-key/schema and existing worker contracts.

A concurrent retry can finish during job chunk grouping before the first job
exists. Atomic enqueue therefore also compares the planned identity glossary
with the current analysis terms and returns `analysis_pending` on a stale
snapshot. The worker reads the remaining plan fields after insertion, when the
first job has frozen them. Focused regressions cover both readiness and terms
changing between initial planning and aggregate insertion.


## Completion and validation

Implemented DT-36–DT-40 and approved by independent review for both spec
compliance and code quality. `make test`: 367 passed, 2 live tests deselected;
`make lint`: clean, 113 files formatted; `make typecheck`: clean, 49 source
files. Existing Pydantic register-shadow warnings remain. Threaded tests ran
outside the sandbox. No live OpenAI calls were made; live verification remains
opt-in via `make test-live`. Independent review additionally exercised the real
SDK tool-calling loop with an offline mock transport, verified structured
output, and confirmed opaque metadata never entered provider requests.

AI execution log: the adapter implementer delivered bounded navigation, both
adapters and 48 passing offline tests. The orchestrator integrated background
triage and added lifecycle/race tests. Independent review rejected mutable
analysis after enqueue by reproducing reuse of degraded translations after a
plan change; the correction freezes analyses after any job and validates the
current term snapshot during atomic insertion. Root checks confirmed all
regressions, lifecycle behavior and delivery scope. No reviewer/implementer
commits were made; the orchestrator owns implementation and task-hash commits.
