# Analysis cost in translation history — implementation plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Show what the triage agent cost in the history view, once per document,
labelled as shared, without changing `JobRecord`.

**Architecture:** `analysis_cost_usd` is added to `JobSummaryResponse` and
resolved in the API layer with a single batched query over the distinct
`document_id` values of the returned jobs. The frontend renders the line on the
first card of each document group and omits it when the cost is zero.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0 async (aiosqlite), Pydantic
v2, React 19 + TypeScript + Vite, pytest, Vitest. No new dependency, no schema
migration.

**Ticket:** **DT-103** (DT-102 was already used for the owner-selected triage default).

---

## Origin

The bulk translation cost is shown per job, but the agent call that produced the
`TranslationPlan` is invisible outside one page. `analysis_cost_usd` already
exists on `DocumentUploadResponse` (`app/api/routers/documents.py:32`, sourced
from `document_analyses.cost_usd_total`), is already validated by the client
(`frontend/src/api/client.ts:16` via `money()`), and is already rendered on
`BatchPage`. It is simply absent from history, where `JobSummaryResponse` carries
no such field.

Two structural facts shaped the design, both verified in the repository rather
than assumed:

- `JobRecord` maps one-to-one to the `jobs` table columns
  (`ARCHITECTURE.md:241`), and `cost_usd_total` lives in `document_analyses`. The
  field therefore belongs in the response schema and is resolved in the API
  layer; `JobRecord` must not grow it.
- On live data the last 10 jobs span **7 unique documents**, so grouping by
  document saves little traffic. The batching therefore exists to avoid an N+1,
  not to reduce request count — and duplicate suppression is a rendering concern,
  not a data concern.

## Owner decisions

1. **Not on the upload page.** The cost becomes known in the same transaction that
   moves the document to `extracted`, and the hook navigates to the batch page
   immediately afterwards, so the figure would be on screen for a fraction of a
   second.
2. **Source of truth:** a field on `JobSummaryResponse`, with duplicate
   suppression in the renderer.
3. **Label:** a separate line marked as shared, never merged into `Cost:`.
4. **Zero is hidden.** A `$0.0000` line on every legacy document would bury the
   real figure.

## Invariants this plan must not break

1. **`JobRecord` is unchanged.** The 1:1 record-to-column mapping holds; the new
   field exists only in the response schema.
2. **No per-job query.** Analysis cost is resolved in one batched statement for a
   whole list, never inside a loop.
3. **`extra="forbid"` preserved** on both the Pydantic response and the client
   parser: an unknown or malformed field is rejected, not silently ignored.
4. **Zero renders nothing** (decision 4).
5. **No leakage.** No path, document text, or session identifier enters the new
   field or any log line.
6. **Accessibility.** The new line carries an accessible name; it is not conveyed
   by colour or icon alone.
7. **One source for both pages.** History and `BatchPage` read the same
   `cost_usd_total`, so the two can never disagree.

---

## Phase 1 — Backend: batched resolution

**Files:**
- Modify: `app/adapters/persistence/api.py` (`ApiPersistence`)
- Modify: `app/api/routers/jobs.py` (`_summary` and its five call sites)
- Modify: `app/api/schemas.py` (`JobSummaryResponse`)
- Test: `tests/api/test_jobs_api.py`, `tests/test_api_schemas.py`

**Step 1 — write the failing test**

```python
async def test_job_list_reports_the_shared_analysis_cost(client) -> None:
    # Two jobs for one document, one job for another document.
    body = (await client.get("/api/jobs")).json()
    by_id = {job["id"]: job for job in body}
    assert by_id[job_de]["analysis_cost_usd"] == 0.0014
    assert by_id[job_fr]["analysis_cost_usd"] == 0.0014  # same document
    assert by_id[job_other]["analysis_cost_usd"] == 0.0  # never analysed


async def test_analysis_cost_uses_one_query_for_the_whole_list(client, counting_connection) -> None:
    await client.get("/api/jobs?limit=10")
    statements = [
        sql
        for sql in counting_connection.statements
        if "document_analyses" in sql and "cost_usd_total" in sql
    ]
    assert len(statements) == 1
```

The second test is the important one: it pins invariant 2. Check how
`tests/api/test_jobs_api.py` already observes SQL — if it has no connection
hook, add one rather than inventing a second mechanism.

**Step 2 — run and watch it fail**

```bash
uv run pytest tests/api/test_jobs_api.py -q -k analysis_cost
```

Expected: `KeyError: 'analysis_cost_usd'`.

**Step 3 — implement the batched lookup**

`ApiPersistence`:

```python
    async def analysis_costs(self, document_ids: Sequence[str]) -> dict[str, float]:
        """Resolve cumulative triage cost per document in one query."""
        unique = sorted({value for value in document_ids if value})
        if not unique:
            return {}
        placeholders = ",".join("?" * len(unique))
        async with self._connection.execute(
            f"SELECT document_id, cost_usd_total FROM document_analyses "
            f"WHERE document_id IN ({placeholders})",
            tuple(unique),
        ) as cursor:
            rows = await cursor.fetchall()
        return {str(row["document_id"]): float(row["cost_usd_total"]) for row in rows}
```

Placeholders are built from the count, never from values, so a `document_id` can
never reach the SQL text. The leading underscore in the method name signals it is
an internal API-persistence helper rather than a core port, consistent with the
rest of that class.

Give `_summary` an optional second argument so the other call sites keep working:

```python
def _summary(job: JobRecord, analysis_cost_usd: float = 0.0) -> JobSummaryResponse:
    return JobSummaryResponse(
        ...,
        cost_usd=job.cost_usd,
        analysis_cost_usd=analysis_cost_usd,
        error=_safe_job_error(job.error_code),
    )
```

Resolve in `list_recent_jobs` (`jobs.py:57`):

```python
    jobs = await service.list_recent_jobs(limit)
    costs = await persistence.analysis_costs([job.document_id for job in jobs])
    return [_summary(job, costs.get(job.document_id, 0.0)) for job in jobs]
```

Resolve the same way in `get_batch` (`jobs.py:169`) for its jobs. Leave `get_job`
and `retry_job` (`jobs.py:68`, `jobs.py:80`) on the `0.0` default for now — a
single card has no sibling group to mislead, and the batch view already shows the
figure.

**Step 4 — extend the schema**

```python
class JobSummaryResponse(BaseModel):
    ...
    cost_usd: float
    analysis_cost_usd: float = Field(default=0.0, ge=0.0)
    error: JobError | None
```

`ge=0.0` rejects a negative cost at the contract rather than letting it reach a
screen. The default keeps the untouched `_summary` call sites valid.

**Step 5 — run**

```bash
uv run pytest tests/api tests/test_api_schemas.py -q
```

**Commit only when the owner asks:**
`DT-103: feat(api): report shared document analysis cost on job payloads`

---

## Phase 2 — Frontend: types and validation

**Files:**
- Modify: `frontend/src/api/types.ts`
- Modify: `frontend/src/api/client.ts` (`parseProgress`)
- Test: `frontend/src/api/client.test.ts`

**Step 1 — write the failing tests**

Extend the malformed-payload matrix in `client.test.ts`:

```ts
{ ...job, analysis_cost_usd: '0.0014' },
{ ...job, analysis_cost_usd: -1 },
{ ...job, analysis_cost_usd: Number.POSITIVE_INFINITY },
{ ...job, analysis_cost_usd: undefined },
```

**Step 2 — run and watch it fail**

```bash
npm --prefix frontend test -- client
```

**Step 3 — implement**

Add `analysis_cost_usd: number;` to `JobSummaryResponse` in `types.ts`. In
`parseProgress`, which both the REST and SSE parsers share:

```ts
|| !money(value.analysis_cost_usd)) return null;
...
analysis_cost_usd: value.analysis_cost_usd,
```

**Step 4 — run**

```bash
npm --prefix frontend test -- client
```

**Commit only when the owner asks:**
`DT-103: feat(frontend): accept the shared analysis cost field`

---

## Phase 3 — Frontend: duplicate suppression and rendering

**Files:**
- Modify: `frontend/src/features/history/HistoryPage.tsx`
- Modify: `frontend/src/features/jobs/JobCard.tsx`
- Test: `frontend/src/features/history/HistoryPage.test.tsx`,
  `frontend/src/features/jobs/JobCard.test.tsx`

**Step 1 — write the failing tests**

```tsx
it('shows the analysis cost once per document and marks it shared', async () => {
  vi.mocked(api.listRecentJobs).mockResolvedValue([
    { ...completed, id: 'job-de', document_id: 'doc-a', analysis_cost_usd: 0.0014 },
    { ...completed, id: 'job-fr', document_id: 'doc-a', analysis_cost_usd: 0.0014 },
    { ...completed, id: 'job-es', document_id: 'doc-b', analysis_cost_usd: 0.0 },
  ]);
  renderPage();
  const line = await screen.findByText(/Document analysis/);
  expect(line).toHaveTextContent('$0.0014');
  expect(line).toHaveTextContent('shared by 2 translations');
  expect(screen.getAllByText(/Document analysis/)).toHaveLength(1);
});

it('hides the analysis line when the cost is zero', async () => {
  vi.mocked(api.listRecentJobs).mockResolvedValue([
    { ...completed, analysis_cost_usd: 0.0 },
  ]);
  renderPage();
  await screen.findByRole('article');
  expect(screen.queryByText(/Document analysis/)).not.toBeInTheDocument();
});

it('uses the singular form for a single translation', async () => {
  vi.mocked(api.listRecentJobs).mockResolvedValue([
    { ...completed, document_id: 'doc-a', analysis_cost_usd: 0.0014 },
  ]);
  renderPage();
  expect(await screen.findByText(/shared by 1 translation$/)).toBeInTheDocument();
});
```

The zero test is a regression guard for decision 4; without it the natural later
refactor is "always show the line".

**Step 2 — run and watch them fail**

```bash
npm --prefix frontend test -- HistoryPage
```

**Step 3 — implement grouping in `HistoryPage`**

```tsx
  const seen = new Set<string>();
  ...
  {visibleJobs.map((job) => {
    const firstOfDocument = !seen.has(job.document_id);
    seen.add(job.document_id);
    const sharedBy = visibleJobs.filter((item) => item.document_id === job.document_id).length;
    return <li key={job.id}>
      <JobCard jobId={job.id} initialJob={job} live={false} showDetailsLink onJobUpdate={updateJob}
               analysisCostUsd={firstOfDocument ? job.analysis_cost_usd : null}
               analysisSharedBy={sharedBy} />
      <Link to={`/batches/${encodeURIComponent(job.batch_id)}`} className="detail-link mt-3">View batch</Link>
    </li>;
  })}
```

`visibleJobs` must be computed before the map so the group count reflects the
current filter, not the unfiltered list.

**Step 4 — render in `JobCard`**

```tsx
{analysisCostUsd !== null && analysisCostUsd > 0 && (
  <p className="mt-2 text-neutral-300"
     aria-label={`Document analysis cost, shared by ${analysisSharedBy} translations`}>
    Document analysis: <span>${analysisCostUsd.toFixed(4)}</span>
    <span className="block text-sm">
      shared by {analysisSharedBy} {analysisSharedBy === 1 ? 'translation' : 'translations'}
    </span>
  </p>
)}
```

Both props are **optional with a `null` default**, so `JobCard` stays usable from
`BatchPage` and the job detail page, where no grouping exists, and their existing
tests keep passing untouched.

**Step 5 — run**

```bash
npm --prefix frontend test
npm --prefix frontend run build
```

The build matters: the frontend is compiled into the image, so a type error breaks
the image.

**Commit only when the owner asks:**
`DT-103: feat(frontend): show the shared analysis cost in history`

---

## Phase 4 — Documentation

**Files:** `ARCHITECTURE.md`, `DECISIONS.md`, `docs/ops.md`

**Step 1 — contract.** In *REST API surface*, document `analysis_cost_usd` on the
job payload: sourced from `document_analyses.cost_usd_total`, cumulative across
every triage attempt for that document, and explicitly *not* per job or per
language.

**Step 2 — grouping semantics.** Record that the cost is shared by all languages
of one document and is rendered once, so a reader does not conclude that three
jobs cost three times as much.

**Step 3 — known limitation.** For documents analysed before usage
instrumentation, `cost_usd_total` is `0`, and zero means "not recorded" rather
than "free". The UI hides those rows; the limitation belongs in the docs, not only
in the absence of a line.

**Commit only when the owner asks:**
`DT-103: docs: describe the shared analysis cost field`

---

## Phase 5 — Verification

```bash
uv run pytest tests/api tests/test_api_schemas.py -q
npm --prefix frontend test
make test && make lint && make typecheck
docker compose up -d --build
```

Manual check on the running stack: open `/history` and confirm the line appears
once per document group, carries the shared count, and is absent for documents
whose analysis predates instrumentation.

## Verification matrix

| Requirement | Test | Status |
|---|---|---|
| Field present in the job list payload | `tests/api/test_jobs_api.py` | new |
| One SQL statement regardless of job count | `tests/api/test_jobs_api.py` | new |
| Jobs of one document report the same figure | `tests/api/test_jobs_api.py` | new |
| Default `0.0` for unresolved summaries | `tests/test_api_schemas.py` | new |
| Negative cost rejected by the schema | `tests/test_api_schemas.py` | new |
| Client rejects malformed cost payloads | `frontend/src/api/client.test.ts` | new |
| Line rendered once per group, marked shared | `HistoryPage.test.tsx` | new |
| Zero renders no line | `HistoryPage.test.tsx` | new |
| Singular/plural wording | `HistoryPage.test.tsx` | new |
| `BatchPage` and job detail unaffected | existing tests | existing |
| `JobRecord` untouched | `tests/test_core_models.py`, `test_domain_contracts.py` | existing |

## Out of scope

- The upload page. Decision 1, and the figure would be on screen for a fraction
  of a second because the analysis commit and the navigation are adjacent.
- A new documents endpoint; `JobSummaryResponse` is sufficient.
- Totals across a batch, or a "spent so far" figure.
- Breaking the analysis cost down by attempt, token count, or model.
- Changing `BatchPage`, which already renders the figure correctly.
- Backfilling `cost_usd_total` for documents analysed before instrumentation.

## Execution notes

- **No commit without an explicit request.**
- Phase 1 adds one statement per job-list request. At this scale it is
  unmeasurable; if the history list ever grows, folding the same value into the
  existing `SELECT` in `list_recent_jobs` with a join is the cheaper shape.
- `JobRecord` stays untouched under all circumstances — it is a recorded
  invariant, not a preference.
- If the upload page is revisited later, it needs its own decision: the cost
  becomes known in the transaction that sets `extracted`, and the hook navigates
  to the batch page immediately afterwards.