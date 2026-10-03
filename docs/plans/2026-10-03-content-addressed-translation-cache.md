# Content-addressed translation cache — implementation plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the translation cache actually deduplicate unchanged paragraphs
across documents, key it by content instead of by document-scoped block ID, key
it also by the analysis plan so translations cannot leak across domains, and
export durable hit/miss counters so the saving is measurable instead of asserted.

**Architecture:** The cache key becomes
`hash(target_language, model, prompt_version, glossary_hash, plan_hash)`, and the
per-block identity becomes the block's existing `source_hash`
(`sha256(source_text)`) instead of `block_id`. `block_id` stays
document-scoped, because `blocks.id` is a global primary key and the provider
adapter rejects duplicate block IDs inside one request. That single change turns
"re-execution of the same job" into "translation of unchanged text anywhere",
which is the behaviour `ARCHITECTURE.md` already promised and the code never
implemented.

Cache hits are counted **per job** in two durable `jobs` columns
(`cache_hit_blocks`, `cache_miss_blocks`) rather than in a process-level counter.
That single source then feeds three consumers: the `/metrics` Prometheus
export (aggregated with `SUM`), the REST/SSE job payloads, and the web UI, which
renders "N% cached" live during translation. Per-job counters are what make the
demo visible — re-uploading a near-identical document shows a high percentage and
a near-zero cost delta instead of an unfalsifiable claim.

**Tech Stack:** Python 3.12, SQLAlchemy 2.0 async (aiosqlite), Pydantic v2,
structlog, prometheus_client, pytest. No new dependency.

**Ticket:** DT-91 (stage 11 — defects found in operation).

---

## Origin: what the audit found

Two documents that differ by two words are currently translated at **100% cost**,
twice. Traced through the code:

| Step | Location | Effect of a 2-word edit |
|---|---|---|
| `document_id = sha256(<all file bytes>)` | `app/core/services/document_service.py:87` | new document |
| `block_id = uuid5(NAMESPACE_URL, f"{document_id}:{seq}")` | `app/adapters/formats/pdf.py:95`, `docx.py:67`, `markdown.py:223` | **every** block ID changes, including untouched paragraphs |
| idempotency key contains `document_id` | `app/core/services/job_service.py:106` | new job |
| `get_block_translation(translation_key, block.id)` | `app/worker/translation_loop.py:75` | **miss on every block** |

The cache therefore only ever served its real purpose — deduplicating *work*
(re-execution, provider retry, resume after `kill -9`) — and never deduplicated
*text*. `Block.source_hash` is computed by all three format adapters and stored
in `blocks.source_hash`, but no cache path reads it.

Three documented claims are false today:

- `ARCHITECTURE.md:282-285` — "Combined with the block's own `source_hash`
  identity, the cache key reflects **every semantic input** … A repeated
  paragraph — within one document or across jobs — is translated once."
- `ARCHITECTURE.md:63-67` — "repeated blocks within a document also use the
  block cache". Two identical paragraphs in one document get different `seq`,
  therefore different IDs, therefore a miss.
- `ARCHITECTURE.md:173` — `source_hash: str  # cache identity`. It is not in the
  cache identity.

A second, independent hole sits behind the first: `translation_key`
(`app/worker/keys.py:8-17`) hashes language, model, prompt version and glossary
— but **not** `TranslationPlan`, even though the whole plan is rendered into the
provider prompt (`app/adapters/llm/openai_provider.py:222-236`). Today this is
harmless because the cache never hits across documents. The moment it does, a
paragraph translated under `domain=legal` would be reused for
`domain=technical`. Fixing the cache key without the plan hash would ship that
bug, so both land in DT-91.

---

## Invariants this plan must not break

1. **`block_id` stays document-scoped.** `blocks.id` is a global primary key
   (`schema.sql:18`) and `chunk_blocks.block_id`/`block_translations.block_id`
   reference it. Making IDs content-derived would collide across documents and
   trip `openai_provider.py:73-75`, which rejects duplicate IDs in one request.
2. **Every semantic prompt input stays covered.** Language, model, prompt
   version, glossary and the *entire* plan (`source_language`, `domain`,
   `register`, `terms`, `warnings`, `triage_status`) — the plan is hashed exactly
   as it is rendered (`plan.model_dump(mode="json")`).
3. **Source text enters only through `source_hash`.** One changed paragraph must
   not invalidate its neighbours.
4. **SQL stays in `app/adapters/persistence/`** (AGENTS.md rule 1). The core
   keeps SQL-free ports; `app/core/` still imports nothing from `worker/`.
5. **No provider call is removed for a block whose translation is not already
   durable.** A cache hit must mean "row exists in SQLite", never "in memory".
6. **No document or job behaviour changes for byte-identical uploads.** Those
   are already idempotent and must stay free.
7. **Metrics stay honest.** Counters are durable and job-scoped; a rate is
   derived from counts, never estimated, and the global rate is a `SUM` over jobs
   rather than an in-process counter that resets on restart.
8. **No document or block text reaches logs or metrics labels.**
9. **The UI must not overstate the granularity.** The cache is keyed per
   **block**, never per chunk. The UI says "blocks", never "chunks", and the
   denominator is `hits + misses` — blocks actually looked up — not
   `total_chunks`, which counts execution units and includes structural blocks
   that are never sent anywhere.
10. **The API publishes counts, not a ratio.** Two integers leave the backend;
    the percentage is derived in the client. A server-side float would add a
    second definition of the same number and a rounding disagreement between the
    badge and the tooltip.

---

## Decisions taken (owner-approved)

- **Option C:** content-addressed cache **and** `plan_hash` in the key.
- **Context is not part of the key.** If a paragraph's text is unchanged its
  translation is reused even when its neighbours changed. Recorded in
  `DECISIONS.md` with the trade-off stated.
- **Durable counters** `cache_hits_total` and `cache_misses_total` ship in this
  ticket.
- **`ARCHITECTURE.md` claims are corrected**, not deleted — the section keeps
  its intent and gains the real mechanics.

### Rejected alternatives

1. **Content-derived `block_id`.** Rejected: collides with the `blocks` primary
   key and with the provider's duplicate-ID rejection (invariant 1).
2. **Fuzzy / embedding cache.** Rejected: out of scope for a translation cache
   whose correctness rests on exact-match reuse; "similar" is not "identical" for
   legal and technical text. Belongs in `DECISIONS.md` future work.
3. **Hash the neighbours into the key.** Rejected by owner: any neighbouring
   edit would invalidate the block, destroying the benefit.
4. **Backfill the old cache table.** Rejected — see Task 2. The new key can
   never equal the old key, so migrated rows would be unreadable garbage.

---

## Task 1: Single-source cache-key module

Two implementations of the same hash exist today (`app/worker/keys.py` and
`app/core/services/job_service.py:361`). They must not diverge, and `core/`
cannot import from `worker/`, so the function moves into `core/`.

**Files:**
- Create: `app/core/services/cache_keys.py`
- Delete: `app/worker/keys.py`
- Delete: `tests/worker/test_keys.py` (replaced by Task 1's test)
- Create: `tests/services/test_cache_keys.py`

**Step 1 — write the failing test**

`tests/services/test_cache_keys.py`:

```python
"""Cache identity covers every semantic input and nothing else."""

import pytest

from app.core.models import TriageStatus, TranslationPlan
from app.core.services.cache_keys import translation_key


def _plan(**changes: object) -> TranslationPlan:
    base = TranslationPlan(
        source_language="en",
        domain="general",
        register="neutral",
        terms=["invoice"],
        warnings=[],
    )
    return base.model_copy(update=changes)


def test_key_ignores_job_identity_and_glossary_order() -> None:
    plan = _plan()
    first = translation_key("de", "gpt-4o-mini", "v1", {"B": "b", "A": "a"}, plan)
    second = translation_key("de", "gpt-4o-mini", "v1", {"A": "a", "B": "b"}, plan)
    assert first == second


@pytest.mark.parametrize(
    "change",
    [
        {"target_language": "uk"},
        {"model": "other"},
        {"prompt_version": "v2"},
        {"glossary": {"A": "changed"}},
    ],
)
def test_key_changes_with_every_non_plan_input(change: dict[str, object]) -> None:
    plan = _plan()
    arguments = {
        "target_language": "de",
        "model": "gpt-4o-mini",
        "prompt_version": "v1",
        "glossary": {},
        "plan": plan,
    }
    arguments.update(change)
    assert translation_key(**arguments) != translation_key("de", "gpt-4o-mini", "v1", {}, plan)


@pytest.mark.parametrize(
    "plan_change",
    [
        {"source_language": "de"},
        {"domain": "legal"},
        {"register": "formal"},
        {"terms": ["invoice", "due"]},
        {"warnings": ["ambiguous table"]},
        {"triage_status": TriageStatus.DEGRADED},
    ],
)
def test_key_changes_with_every_rendered_plan_field(plan_change: dict[str, object]) -> None:
    assert translation_key("de", "m", "v1", {}, _plan()) != translation_key(
        "de", "m", "v1", {}, _plan(**plan_change)
    )
```

**Step 2 — run it and watch it fail**

`uv run pytest tests/services/test_cache_keys.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'app.core.services.cache_keys'`.

**Step 3 — implement**

`app/core/services/cache_keys.py`:

```python
"""Stable semantic cache identity, independent of job, document and block IDs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from app.core.models import TranslationPlan


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def plan_hash(plan: TranslationPlan) -> str:
    """Hash the plan exactly as it is rendered into the provider prompt.

    ``openai_provider._build_messages`` serializes ``plan.model_dump(mode="json")``
    verbatim, so every field — including ``triage_status`` — is an input and is
    hashed. Key order is canonicalized; list order is preserved because it is
    rendered verbatim and therefore semantically observable.
    """
    return _digest(_canonical(plan.model_dump(mode="json")))


def translation_key(
    target_language: str,
    model: str,
    prompt_version: str,
    glossary: Mapping[str, str],
    plan: TranslationPlan,
) -> str:
    """Content-addressed cache identity for one translated block.

    Source text is deliberately absent: it enters at lookup time through the
    block's own ``source_hash``, so editing one paragraph cannot invalidate its
    neighbours. Everything else the provider prompt depends on — target
    language, model, prompt version, glossary content and the analysis plan — is
    hashed here.
    """
    return _digest(
        _canonical(
            [
                target_language,
                model,
                prompt_version,
                _digest(_canonical(dict(glossary))),
                plan_hash(plan),
            ]
        )
    )
```

**Step 4 — run it again**

`uv run pytest tests/services/test_cache_keys.py -q` → Expected: all pass.

**Step 5 — check `TriageStatus` exists**

`uv run python -c "from app.core.models import TriageStatus; print(list(TriageStatus))"`
If the member is not named `DEGRADED`, use the actual name in the test — do not
add an enum member in this task.

**Commit only when the owner asks:**
`DT-91: refactor(cache): centralize plan-aware translation cache key`

---

## Task 2: Schema and migration

**Files:**
- Modify: `app/adapters/persistence/schema.sql:105-114`
- Modify: `app/adapters/persistence/database.py` (new `_migrate_translation_cache`,
  called next to `_migrate_document_analysis_usage` at `database.py:186`)
- Test: `tests/adapters/persistence/test_database.py`,
  `tests/adapters/persistence/test_schema.py`

**Step 1 — write the failing migration test**

In `tests/adapters/persistence/test_database.py`, build a legacy database by
hand with the current `block_translations` DDL and a row in it, open it through
`SqliteConnectionFactory`, then assert:

```python
    columns = {str(row[1]) for row in await _all(connection, "PRAGMA table_info(block_translations)")}
    assert "source_hash" in columns
    assert "block_id" not in columns
    # The old key can never be produced again, so rows are dropped, not migrated.
    assert await _scalar(connection, "SELECT COUNT(*) FROM block_translations") == 0
    indexes = await _all(
        connection,
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='block_translations'",
    )
    assert {str(row[0]) for row in indexes} == {"idx_block_translations_lookup"}
```

Reuse the legacy-DB construction style already present in
`test_database.py:62-117` (`sqlite3.connect` + `executescript`), and the
`_scalar` helper that file already defines. Also assert the two new job columns
exist with a `0` default:

```python
columns = {str(row[1]): row[4] for row in await _all(connection, "PRAGMA table_info(jobs)")}
assert columns["cache_hit_blocks"] == 0
assert columns["cache_miss_blocks"] == 0
```

**Step 2 — run it and watch it fail**

`uv run pytest tests/adapters/persistence/test_database.py -q`
Expected: failure — `block_id` still present, cache columns missing.

**Step 3 — update `schema.sql`**

Replace the `block_translations` block with:

```sql
-- Translation cache, content-addressed: one row per (semantic inputs, source
-- text). block_id is deliberately absent — it is document-scoped, so keying on
-- it could never deduplicate text across documents.
CREATE TABLE IF NOT EXISTS block_translations (
    translation_key TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    translated_text TEXT NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (translation_key, source_hash)
);

CREATE INDEX IF NOT EXISTS idx_block_translations_lookup
    ON block_translations(translation_key, source_hash);
```

Add two columns to the `jobs` table, next to `done_chunks` — per-job cache
counters, durable and monotonic. They are the single source for the `/metrics`
export, the REST/SSE payloads and the UI badge:

```sql
    cache_hit_blocks INTEGER NOT NULL DEFAULT 0,
    cache_miss_blocks INTEGER NOT NULL DEFAULT 0,
```

**Step 4 — add the migration**

In `app/adapters/persistence/database.py`, next to the existing migration, add a
function following the same two-phase shape (read-only inspection first, then
`BEGIN IMMEDIATE` with a recheck):

```python
async def _migrate_translation_cache(connection: Connection) -> None:
    """Move the cache to source-hash identity and add per-job cache counters.

    Legacy cache rows are dropped, not copied. The new key hashes the analysis
    plan in addition to the previous inputs, so it can never equal an old key and
    every migrated row would be permanently unreadable. Losing them costs one
    re-translation of jobs that were in flight across the deploy.

    Inspects the schema without a write lock and re-checks after acquiring
    ``BEGIN IMMEDIATE``, so a concurrent initializer observes another
    process's completed migration and skips it.
    """

    async def legacy_cache() -> bool:
        async with connection.execute("PRAGMA table_info(block_translations)") as cursor:
            columns = {str(row[1]) for row in await cursor.fetchall()}
        return bool(columns) and "block_id" in columns

    async def missing_counters() -> list[str]:
        async with connection.execute("PRAGMA table_info(jobs)") as cursor:
            existing = {str(row[1]) for row in await cursor.fetchall()}
        return [name for name in ("cache_hit_blocks", "cache_miss_blocks") if name not in existing]

    if not await legacy_cache() and not await missing_counters():
        return

    async with transaction(connection):
        if await legacy_cache():
            await connection.execute("DROP TABLE block_translations")
            await connection.execute(
                "CREATE TABLE block_translations ("
                "translation_key TEXT NOT NULL, source_hash TEXT NOT NULL, "
                "translated_text TEXT NOT NULL, "
                "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                "PRIMARY KEY (translation_key, source_hash))"
            )
            await connection.execute(
                "CREATE INDEX idx_block_translations_lookup "
                "ON block_translations(translation_key, source_hash)"
            )
        for name in await missing_counters():
            await connection.execute(
                f"ALTER TABLE jobs ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0"
            )
```

Call it from the initializer immediately after
`_migrate_document_analysis_usage(connection)` (`database.py:186`), inside the
same `if self._init_schema:` block. Mirror the existing function's fast path
exactly — the two read-only `PRAGMA` inspections must happen **before** any
write lock is taken, and the recheck inside the transaction must re-run both
helpers, because a concurrent initializer may have finished the work in between.

Note the ordering dependency: `executescript(schema)` runs first and its
`CREATE TABLE IF NOT EXISTS` is a no-op on a legacy database, so the migration
is what actually reshapes the table and recreates the index.

**Step 5 — add schema-level tests**

In `tests/adapters/persistence/test_schema.py`, assert that a fresh database has
`source_hash` and no `block_id` on `block_translations`, that the primary key is
`(translation_key, source_hash)`, and that inserting the same
`(translation_key, source_hash)` twice raises `IntegrityError`. Follow the
existing style in that file.

**Step 6 — run**

`uv run pytest tests/adapters/persistence -q` → Expected: all pass.

**Commit only when the owner asks:**
`DT-91: feat(persistence): address the translation cache by source hash`

---

## Task 3: Cache repository and port

**Files:**
- Modify: `app/core/ports.py:161-174` (`TranslationCacheRepository`)
- Modify: `app/adapters/persistence/repositories.py:681-712`
- Test: `tests/adapters/persistence/test_repositories.py`

**Step 1 — write the failing test**

```python
async def test_cache_lookup_is_scoped_by_key_and_source_hash() -> None:
    async with connection() as conn:
        repo = SqliteTranslationCacheRepository(conn)
        await repo.save_block_translation("key-a", "hash-1", "Hallo")
        assert await repo.get_block_translation("key-a", "hash-1") == "Hallo"
        # Same text, different plan/language key: no reuse across keys.
        assert await repo.get_block_translation("key-b", "hash-1") is None
        assert await repo.get_block_translation("key-a", "hash-2") is None
        # Concurrent double execution must not commit twice.
        await repo.save_block_translation("key-a", "hash-1", "Hallo")
        assert (
            await _scalar(
                conn, "SELECT COUNT(*) FROM block_translations WHERE source_hash = 'hash-1'"
            )
            == 1
        )
```

Use the file's existing connection fixture rather than inventing one.

**Step 2 — run and watch it fail**

`uv run pytest tests/adapters/persistence/test_repositories.py -q`
Expected: failure — the lookup still keys on `block_id`, so the row is not found.

**Step 3 — change the port** (`app/core/ports.py`)

```python
class TranslationCacheRepository(Protocol):
    async def get_block_translation(
        self,
        translation_key: str,
        source_hash: str,
    ) -> str | None: ...

    async def save_block_translation(
        self,
        translation_key: str,
        source_hash: str,
        translated_text: str,
    ) -> None: ...
```

**Step 4 — implement** (`app/adapters/persistence/repositories.py`)

Rename the `block_id` parameters to `source_hash`, change the `WHERE` and
`INSERT` column lists to match the new table, and keep `INSERT OR IGNORE`. Do not
add counter methods here — cache accounting belongs to the job row (Task 4), not
to the cache table, so that one `UPDATE jobs` serves the metrics export, the API
and the UI.

**Step 5 — run**

`uv run pytest tests/adapters/persistence -q` → Expected: all pass.

**Commit only when the owner asks:**
`DT-91: feat(persistence): key the translation cache by source hash`

---

## Task 4: Worker wiring — content lookup and per-job counters

**Files:**
- Modify: `app/worker/translation_loop.py:74-107, 316-350`
- Modify: `app/core/ports.py` (`JobExecutionRepository`), and its SQLite
  implementation in `app/adapters/persistence/repositories.py`
- Test: `tests/worker/test_translation_loop.py`

**Step 1 — write the failing test**

Add to `tests/worker/test_translation_loop.py`:

```python
async def test_cache_lookup_counts_hits_and_misses_per_job(fixture) -> None:
    job, chunk, blocks, plan = fixture["job"], fixture["chunk"], fixture["blocks"], fixture["plan"]
    key = translation_key(job.target_language, job.model, job.prompt_version, job.glossary, plan)
    await fixture["cache_repo"].save_block_translation(key, blocks[0].source_hash, "cached")
    await fixture["loop"].process_chunk(job, chunk, blocks[:2], blocks, plan)
    refreshed = await fixture["job_repo"].get_job(job.id)
    assert (refreshed.cache_hit_blocks, refreshed.cache_miss_blocks) == (1, 1)
```

and a sibling test asserting a fully cached chunk performs no provider call and
records `(len(blocks), 0)`.

**Step 2 — run and watch it fail**

`uv run pytest tests/worker/test_translation_loop.py -q`
Expected: failure — the counters do not exist yet.

**Step 3 — add the port method**

`JobExecutionRepository`:

```python
async def record_job_cache_counts(self, job_id: str, *, hits: int, misses: int) -> None: ...
```

SQLite implementation, monotonic so concurrent chunk workers cannot lose a count:

```python
    async def record_job_cache_counts(self, job_id: str, *, hits: int, misses: int) -> None:
        if hits < 0 or misses < 0:
            raise ValueError("cache counters cannot be negative")
        if not hits and not misses:
            return
        async with self._connection.execute(
            "UPDATE jobs SET cache_hit_blocks = cache_hit_blocks + ?, "
            "cache_miss_blocks = cache_miss_blocks + ? WHERE id = ?",
            (hits, misses, job_id),
        ):
            pass
```

Add the two fields to `JobRecord` (`app/core/models.py`) next to `done_chunks`,
keeping the documented 1:1 record-to-column mapping, and to the `SELECT`/row
mapping that builds it.

**Step 4 — re-key the lookup and count it** (`app/worker/translation_loop.py`)

Replace the import of the deleted module:

```python
from app.core.services.cache_keys import translation_key
```

In `process_chunk`:

```python
key = translation_key(job.target_language, job.model, job.prompt_version, job.glossary, plan)
async with self._persistence.read():
    cached: dict[str, str] = {}
    for block in ordered_blocks:
        translation = await self._cache_repo.get_block_translation(key, block.source_hash)
        if translation is not None:
            cached[block.id] = translation
    first_attempt_no = await self._persistence.next_attempt_no(chunk.id)

missing = [block for block in ordered_blocks if block.id not in cached]
# Counters are written in their own short transaction: a cache lookup
# never runs inside a write lock, and the counts land on the failure
# paths too. A process death in this window undercounts, never
# double counts, because the terminal chunk checkpoint is what makes a
# chunk non-retryable — a redelivered chunk re-reads the same misses
# only after its predecessor's rows are already committed.
async with self._persistence.write():
    await self._persistence.ensure_owned(job.id, self._settings.worker_id, chunk.id)
    await self._job_repo.record_job_cache_counts(job.id, hits=len(cached), misses=len(missing))
```

Delete the now-duplicated second `key = translation_key(job)` line further down
in the same method.

In `_commit_success`, change the save call to
`await self._cache_repo.save_block_translation(key, block.source_hash, result.translations[block.id])`.

**Step 5 — delete `app/worker/keys.py`**

Its only importer was `translation_loop.py`. Confirm with
`uv run grep -rn "worker.keys\|from app.worker import keys" app/ tests/` → expect
no hits, then `rm app/worker/keys.py`.

**Step 6 — run**

`uv run pytest tests/worker -q` → Expected: all pass after updating any test that
still imports `app.worker.keys`.

**Commit only when the owner asks:**
`DT-91: fix(worker): deduplicate unchanged blocks by content hash`

---

## Task 5: `retry_job` must compute the same plan-aware key

`JobService.retry_job` requeues only chunks that are still uncached, and does it
in SQL (`app/adapters/persistence/api.py:255-266`). It computes the key from
`JobRecord` alone — which has no plan — so it would silently re-translate
everything.

**Files:**
- Modify: `app/core/services/job_service.py:255-278, 361-373`
- Modify: `app/adapters/persistence/api.py:255-266`
- Test: `tests/services/test_job_service.py`, `tests/api/test_jobs_api.py`

**Step 1 — write the failing test**

```python
async def test_retry_reuses_cached_blocks_by_source_hash(...) -> None:
    # Translate a document, retry its job, assert the provider is not called again.
```

Reuse the existing retry fixtures in that file. Before the fix the provider *is*
called again.

**Step 2 — run and watch it fail**

`uv run pytest tests/services/test_job_service.py -q -k retry` → Expected: the
new test fails; the provider is called a second time.

**Step 3 — load the plan in `retry_job`**

```python
    async def retry_job(self, job_id, *, raised_cost_cap_usd=None):
        job = await self._persistence.get_job(job_id)
        if job is None:
            return None
        if job.status not in {JobStatus.FAILED, JobStatus.COMPLETED_WITH_ERRORS}:
            raise self._error(ErrorCode.CONFLICT, 409)
        async with self._persistence.read():
            analysis = await self._document_repo.get_analysis(job.document_id)
        if analysis is None:
            # Jobs cannot be created without an analysis and it is immutable
            # afterwards, so a missing row means corrupted state, not a race.
            raise self._error(ErrorCode.INTERNAL_ERROR, 500)
        plan = TranslationPlan(
            source_language=analysis.source_language,
            domain=analysis.domain,
            register=analysis.register,
            terms=analysis.terms,
            warnings=analysis.warnings,
            triage_status=analysis.triage_status,
        )
        ...
                translation_key=translation_key(
                    job.target_language, job.model, job.prompt_version, job.glossary, plan
                ),
```

Delete `JobService._translation_key` and import `translation_key` from
`app.core.services.cache_keys` instead. Confirm the `TranslationPlan` fields
against the construction already present in `create_jobs`
(`job_service.py:144+`) so the two build identical plans.

**Step 4 — fix the SQL join** (`app/adapters/persistence/api.py:255-266`)

```sql
SELECT links.block_id
FROM chunk_blocks AS links
JOIN blocks AS source ON source.id = links.block_id
LEFT JOIN block_translations AS translations
    ON translations.translation_key = ? AND translations.source_hash = source.source_hash
WHERE links.chunk_id = ? AND translations.source_hash IS NULL
```

**Step 5 — run**

`uv run pytest tests/services tests/api -q` → Expected: all pass.

**Commit only when the owner asks:**
`DT-91: fix(jobs): requeue only content-uncached chunks on retry`

---

## Task 6: Export the counters

**Files:**
- Modify: `app/adapters/persistence/api.py:155-187` (`metrics_snapshot`)
- Modify: `app/api/routers/health.py:41-49`
- Test: `tests/api/test_jobs_api.py:370`

**Step 1 — write the failing test**

```python
async def test_metrics_report_durable_cache_counters(client) -> None:
    # Arrange jobs with cache hits/misses, scrape /metrics, assert
    # cache_hits_total and cache_misses_total are the persisted values and that
    # a second scrape does not double them.
```

**Step 2 — run and watch it fail**

`uv run pytest tests/api/test_jobs_api.py -q -k metrics` → Expected: counters
still reported as a zero placeholder.

**Step 3 — implement**

In `metrics_snapshot`, replace the placeholder with an aggregate over jobs, next
to the existing cost aggregate:

```python
async with self._connection.execute(
    "SELECT COALESCE(SUM(cache_hit_blocks), 0), COALESCE(SUM(cache_miss_blocks), 0) FROM jobs"
) as cursor:
    cache_row = await cursor.fetchone()
```
and return
```python
            "cache_hits_total": 0 if cache_row is None else int(cache_row[0]),
            "cache_misses_total": 0 if cache_row is None else int(cache_row[1]),
```
(dropping the "intentionally not persisted" comment). No job deletion path
exists, so a `SUM` over jobs is equivalent to a lifetime counter and, unlike an
in-process counter, survives a restart.

In `app/api/routers/health.py`, replace the tuple entry with:

```python
(("cache_hits_total", "Blocks served from the translation cache"),)
(("cache_misses_total", "Block translations absent from the cache"),)
```

**Step 4 — run**

`uv run pytest tests/api -q` → Expected: all pass.

**Commit only when the owner asks:**
`DT-91: feat(metrics): export durable cache hit and miss counters`

---

## Task 7: Prove the behaviour end to end

The audit's core claim was that a two-word edit costs 100% of a fresh run. This
task is the regression test that would have caught it.

**Files:**
- Create: `tests/worker/test_cache_reuse.py`

**Step 1 — write the test**

Build two documents through the real DOCX extractor with identical paragraphs
except one, run both through `ClaimLoop` with a recording provider that appends
`(block.source_text for block in request.blocks)`, and assert:

```python
    assert first_calls == ["Alpha paragraph.", "Beta paragraph.", "Gamma paragraph."]
    # The second document differs only in "Beta".
    assert second_calls == ["Beta paragraph, revised."]
    first_job = await job_repo.get_job(first_job_id)
    second_job = await job_repo.get_job(second_job_id)
    assert (first_job.cache_hit_blocks, first_job.cache_miss_blocks) == (0, 3)
    # Three reused blocks, one re-translated block on the second run.
    assert (second_job.cache_hit_blocks, second_job.cache_miss_blocks) == (2, 1)
```

Add a third case: same document text, but the second document's analysis has
`domain="legal"` instead of `"general"` → assert the provider is called for all
blocks and no translation leaks across the plan boundary. That is the
cross-domain contamination guard.

Reuse the stack construction from
`tests/worker/test_worker_integration.py:40-93`; do not duplicate the harness.

**Step 2 — run it against the new code**

`uv run pytest tests/worker/test_cache_reuse.py -q` → Expected: pass.

**Step 3 — prove it fails on the old code**

`git stash push app/ tests/adapters/persistence/test_schema.py` is not safe with
other owners' edits in the tree. Instead, verify the regression differently:
temporarily change `translation_loop.py` back to
`get_block_translation(key, block.id)` (one line), re-run, expect failure with
"all three paragraphs re-sent", then restore. Record the observed failure text.

**Commit only when the owner asks:**
`DT-91: test(worker): prove unchanged paragraphs are served from the cache`

Add the UI-facing assertion here too, so the end-to-end proof covers the badge
and not just the provider calls:

```python
    hits, misses = job.cache_hit_blocks, job.cache_miss_blocks
    assert round(100 * hits / (hits + misses)) == 99
```

---

## Task 8: Expose the counters over REST, SSE and MCP

This is a **public contract addition** (two new response fields). Per AGENTS.md
rule 4 it needs the owner's explicit approval before implementation — the plan
below assumes it is granted.

**Files:**
- Modify: `app/api/schemas.py:23-51` (`JobSummaryResponse`, `ServerSentEvent`)
- Modify: `app/api/routers/jobs.py:116-131, 169-178` (`_summary`, `event_stream`)
- Modify: `app/mcp_server/schemas.py` (`JobSummary`) — *optional, see Step 4*
- Test: `tests/api/test_jobs_api.py`, `tests/test_api_schemas.py`

**Step 1 — write the failing test**

```python
async def test_job_payload_reports_per_job_cache_counts(client) -> None:
    response = await client.get(f"/api/jobs/{job_id}")
    assert response.json()["cache_hit_blocks"] == 12
    assert response.json()["cache_miss_blocks"] == 3
```

and one asserting the SSE `progress` payload carries the same fields, so a live
badge updates without a refetch.

**Step 2 — run and watch it fail**

`uv run pytest tests/api/test_jobs_api.py -q -k cache` → Expected: `KeyError`.

**Step 3 — implement**

```python
class JobSummaryResponse(BaseModel):
    ...
    done_chunks: int
    cache_hit_blocks: int
    cache_miss_blocks: int
    cost_usd: float
```

Same two fields on `ServerSentEvent`. Populate both from `JobRecord` in
`_summary` (`jobs.py:169`) and in the `event_stream` payload
(`jobs.py:120-127`). Non-nullable integers with `0` defaults — a queued job has
`(0, 0)`, and the client must never have to distinguish "absent" from "zero".

Note that `ServerSentEvent` is constructed from `JobRecord` only, so no extra
query is added to the one-second SSE poll.

**Step 4 — MCP, optional but recommended**

`check_status` already reports `done_chunks/total_chunks`; adding
`cache_hit_blocks`/`cache_miss_blocks` to `app/mcp_server/schemas.py::JobSummary`
and `job_summary()` makes the same figure visible to an MCP client such as Codex
— the demo surface the owner cares about. It is the same two integers, so it is
cheap, but it changes an MCP contract: **ask before doing it**.

**Commit only when the owner asks:**
`DT-91: feat(api): report per-job cache hit and miss counts`

---

## Task 9: Show the cache percentage in the UI

**Files:**
- Modify: `frontend/src/api/types.ts:27-56`
- Modify: `frontend/src/api/client.ts:18-24` (strict payload validation)
- Modify: `frontend/src/features/jobs/JobCard.tsx:80-84`
- Modify: `frontend/src/features/jobs/useJobEvents.ts:97`
- Test: `frontend/src/api/client.test.ts`,
  `frontend/src/features/jobs/JobCard.test.tsx`

**Step 1 — write the failing tests**

`client.test.ts` — extend the malformed-payload matrix with
`{ ...job, cache_hit_blocks: '12' }` and `{ ...job, cache_miss_blocks: -1 }`;
both must be rejected, because the validator's job is to keep unvalidated JSON
out of views.

`JobCard.test.tsx`:

```tsx
  it('shows the share of blocks served from the cache', () => {
    render(<JobCard jobId="job-1" />);
    expect(screen.getByText('80% cached')).toBeInTheDocument();
  });

  it('hides the badge when nothing was looked up yet', () => {
    // cache_hit_blocks = 0, cache_miss_blocks = 0 → no "NaN%", no empty bar
  });
```

**Step 2 — run and watch them fail**

`npm --prefix frontend test -- JobCard` → Expected: fail.

**Step 3 — types and validation**

Add `cache_hit_blocks: number;` and `cache_miss_blocks: number;` to
`JobSummaryResponse` and `ServerSentEvent`. Extend `parseProgress` in
`client.ts` — it is shared by the REST and SSE parsers — with
`|| !count(value.cache_hit_blocks) || !count(value.cache_miss_blocks)` and return
both fields. `count()` already enforces a safe non-negative integer, which is
exactly the constraint the badge relies on.

**Step 4 — live updates**

`useJobEvents.ts:97` copies progress fields into the cached job; add the two
counters there so the number climbs during translation without a refetch.

**Step 5 — render**

Add one line under the existing progress row in `JobCard.tsx` (which is also the
detail view — `JobPage.tsx` renders `JobCard`):

```tsx
const cachedTotal = job ? job.cache_hit_blocks + job.cache_miss_blocks : 0;
const cachedPercent = cachedTotal > 0 ? Math.round((job.cache_hit_blocks / cachedTotal) * 100) : null;
...
{job && cachedPercent !== null && (
  <p className="mt-2" aria-label={`${job.cache_hit_blocks} of ${cachedTotal} blocks served from the translation cache`}>
    {cachedPercent}% cached
    <span className="sr-only"> ({job.cache_hit_blocks} of {cachedTotal} blocks reused from previous translations)</span>
  </p>
)}
```

Rules encoded above, all deliberate:

- **Denominator is `hits + misses`**, not `total_chunks`. A document with
  structural Markdown cells has blocks that are never looked up; dividing by
  `total_chunks` would understate the ratio and quietly mix two different
  denominators on one screen.
- **Guard `cachedTotal > 0`** and render nothing when it is zero — no `NaN%`, no
  empty badge for a queued job.
- **Wording says "cached", never "chunks cached".** The unit is blocks; the card
  already shows a separate `done_chunks / total_chunks` line, and conflating the
  two is the kind of overstatement a reviewer will catch.
- The accessible name carries the raw counts, so the percentage is never the
  only way to read the value.

**Step 6 — run**

`npm --prefix frontend test` and `npm --prefix frontend run build` (the Docker
image builds the frontend, so a type error breaks the image build).

**Commit only when the owner asks:**
`DT-91: feat(frontend): show the cached block share during translation`

---

## Task 10: Documentation

**Files:**
- Modify: `ARCHITECTURE.md` — sections *Cost discipline* (63-67), *Core domain
  model* (173), *Persistence* (274-287), *Pipeline* (370-378), *Failure handling
  matrix* (if needed), *Observability* (670-691), *Traceability* (797)
- Modify: `DECISIONS.md` — new numbered record
- Modify: `docs/ops.md` if the metrics list appears there

**Step 1 — correct the persistence section**

Replace the `translation_key` formula and the "every semantic input" paragraph
with the real mechanics: key over language/model/prompt version/glossary/plan,
lookup by `source_hash`, `block_id` deliberately excluded, and the reason
(`blocks.id` is a global primary key and the provider rejects duplicate IDs in a
request).

**Step 2 — correct the cost-discipline claim**

State what is now true: a re-upload that is byte-identical is fully idempotent
and free; an edited document re-translates only blocks whose exact source text
changed; a first-ever document has a 0% hit rate by definition, so hit rate must
be measured over a window.

**Step 3 — correct the observability section**

Replace "present as a zero placeholder pending durable instrumentation" with the
durable per-job counters aggregated by `SUM`, and state the reading rule: totals
are cumulative over all jobs, so a window's hit rate is
`Δhits / (Δhits + Δmisses)` between two scrapes. Record the limitation: a process
death between the lookup and the counter write undercounts; counters never double
count.

**Step 4 — document the new public fields and the UI meaning**

- `ARCHITECTURE.md`, *REST API surface* — add `cache_hit_blocks` and
  `cache_miss_blocks` to the job payload description, stating the unit (blocks,
  not chunks) and that they are cumulative for the job.
- `ARCHITECTURE.md`, *MCP server* — same note if Step 4 of Task 8 is approved.
- `docs/ops.md` — mention that the UI percentage and the two `/metrics` counters
  read the same two columns, so an operator can reconcile what a reviewer sees on
  screen with what Prometheus reports.
- `README.md` — one sentence in the workflow section: re-uploading a document
  whose text is unchanged reuses previous translations and shows the share in the
  UI. Keep it to one sentence; this is a behaviour users can now observe.

**Step 5 — add the decision record to `DECISIONS.md`**

New numbered section following the existing record format, covering:

- *Context* — the audit, the two-word edit costing 100%.
- *Decision* — content-addressed cache with a plan-aware key; **context blocks
  are not part of the key**: if a paragraph's text is unchanged its translation
  is reused even when its neighbours changed, so a translation may have been
  produced with different neighbouring context than the one it now sits in.
- *Why* — provider prompt uses source context only to resolve meaning, and
  paragraph-level self-containment makes the reuse safe in practice; including
  neighbours would invalidate a block whenever anything adjacent changed, which
  is the common case in an edited document.
- *Consequence* — a genuinely context-dependent paragraph (a word whose sense
  depends on the previous one) may be reused in a context where a fresh
  translation would differ. Accepted, measurable via `cache_misses_total`, and
  reversible by adding neighbour hashes later.
- *Measured presentation* — why the UI publishes counts and derives the
  percentage, and why it says "blocks": a percentage over `total_chunks` would
  mix execution units with cache lookups and produce a number that looks
  authoritative but measures nothing.
- *Rejected* — embedding/vector cache, neighbour-hashed keys, a server-computed
  ratio field.

**Step 6 — verify no stale claims remain**

`grep -n "across jobs\|source_hash: str\|cache identity\|zero placeholder" ARCHITECTURE.md`
Expected: every remaining occurrence is either corrected or deliberately quoted.

**Commit only when the owner asks:**
`DT-91: docs(architecture): correct translation cache mechanics and metrics`

---

## Task 11: Delivery

**Step 1 — full suites**

`make test` · `make lint` · `make typecheck`. All must be clean. No
`@pytest.mark.live` test is added; the LLM boundary is untouched.

**Step 2 — live measurement (optional, costs provider calls)**

With `LLM_PROVIDER=fake` the counters still prove cache behaviour at zero cost:
upload `samples/sample_en.pdf`, then a copy with one paragraph reworded, and
compare `cache_hits_total`/`cache_misses_total` deltas at `/metrics`. With a real
provider, re-run the same comparison and record the cost delta from
`llm_cost_usd_total`. Record the actual numbers in `DECISIONS.md` § *Measured
numbers* — the previous "near $0 on repeat" claim was never measured
(`ARCHITECTURE.md:64-65`).

**Step 3 — `TASKS.md`**

Add `| DT-91 | 11 | Content-address the translation cache and export hit/miss counters | done | <hash> |`
plus a stage-11 execution paragraph.

**Step 4 — leave it uncommitted**

The owner has not asked for a commit. Report the diff and wait.

---

## Verification matrix

| Requirement | Test | Status |
|---|---|---|
| Cache key covers language/model/prompt/glossary | `tests/services/test_cache_keys.py` | new |
| Cache key covers every rendered plan field | `tests/services/test_cache_keys.py` | new |
| Key is stable under glossary reordering | `tests/services/test_cache_keys.py` | new |
| Fresh DB has the new table shape | `tests/adapters/persistence/test_schema.py` | new |
| Legacy DB is migrated, rows dropped, index rebuilt | `tests/adapters/persistence/test_database.py` | new |
| Legacy DB gains both job counter columns | `tests/adapters/persistence/test_database.py` | new |
| Duplicate cache writes commit once | `tests/adapters/persistence/test_repositories.py` | new |
| Worker counts hits/misses on the job row | `tests/worker/test_translation_loop.py` | new |
| Retry requeues only content-uncached chunks | `tests/services/test_job_service.py` | new |
| Metrics are durable and not recounted per scrape | `tests/api/test_jobs_api.py` | new |
| Edited document re-sends only changed paragraphs | `tests/worker/test_cache_reuse.py` | new |
| Plan change blocks cross-domain reuse | `tests/worker/test_cache_reuse.py` | new |
| End-to-end percentage matches the provider calls | `tests/worker/test_cache_reuse.py` | new |
| REST job payload reports both counters | `tests/api/test_jobs_api.py` | new |
| SSE progress payload reports both counters | `tests/api/test_jobs_api.py` | new |
| Client rejects malformed counter payloads | `frontend/src/api/client.test.ts` | new |
| Card renders "N% cached" from real counts | `frontend/src/features/jobs/JobCard.test.tsx` | new |
| Card hides the badge at zero lookups (no `NaN%`) | `frontend/src/features/jobs/JobCard.test.tsx` | new |
| No regression in resume/retry/exactly-once | `tests/worker/test_worker_resume.py`, `test_worker_integration.py` | existing |

## Out of scope

- Embedding / vector cache, fuzzy block matching, or per-term glossary diffing.
- Caching triage analysis across documents (triage cost is separately durable;
  near-identical documents still pay for analysis — stated as a known limit).
- Cache eviction, TTL, or garbage collection. The table grows with distinct
  source texts; that is inherent to the design and is recorded as future work.
- Prompt-cache hints to the OpenAI API for chunk translation.
- Any change to `block_id`, chunk packing, or the `_group_blocks` token budget.
- A **per-chunk** cache breakdown in the UI. Only the job-level share is shown;
  chunk-level detail would need per-chunk columns and buys nothing a reviewer
  reads.
- A server-computed percentage or ratio field on the API (see invariant 10).
- Caching of the *rendered* output or any post-translation artifact.
- The DT-90 MCP download error mapping (already delivered as `7f2c224`).

## Execution notes

- **Never commit without an explicit request.** Every commit block above is
  gated on the owner asking.
- **Two contract changes need explicit approval before they are implemented**
  (AGENTS.md rule 4): the two new REST/SSE fields in Task 8, and the optional
  MCP `JobSummary` fields. Task 8's code must not be written before the owner
  says yes, even though the plan documents it.
- Tasks 8 and 9 are only worth doing together. Counters with no UI change the
  backend's shape for nothing a reviewer can see; a UI badge with no durable
  counters would be a number nobody can reproduce.
- The tree already carries unrelated edits (`PROMPTS.md`,
  `docs/assessment_context/roadmap.md`, `OVERVIEW.md`, untracked plan files).
  Stage by explicit path only.
- The migration drops data by design. If the owner prefers preserving
  in-flight cache rows, that requires keeping the old key readable — say so
  before Task 2 rather than during it.
- If Task 7's "prove it fails on the old code" step cannot be done without
  stashing (do not stash — other owners' edits are present), record that the
  regression was instead proven by the audit's code trace and say so in the
  execution record rather than claiming a red test was observed.
- Do not "improve" `plan_hash` with sorted lists in a later task: list order is
  rendered verbatim into the prompt, so sorting would over-deduplicate.
- Do not relabel the UI badge "chunks cached" for punchier wording. The unit is
  blocks, and the same screen already shows a chunk counter; conflating them is
  the fastest way to lose a review.