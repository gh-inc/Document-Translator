# MCP download error mapping — implementation plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Origin:** defect found while operating the system, not a planned stage.
An OpenAI Codex CLI session (v0.160.0, streamable-http MCP client) completed a
full `translate_file` → `check_status` → `download_result` cycle over
`samples/sample_en.pdf` and reported a successful translation with a usable
path, job ID and cost — but every `download_result` call returned
`{"error_code": "invalid_request", "message": "Request validation failed",
"retryable": false, "document_id": null}`. The client, having been told its own
request was malformed, retried the identical call indefinitely.

Ticket: **DT-90** (stage 11 — defects found in operation). DT-79 stays reserved
for the user's separate DOCX table work.

**Two independent faults.** Only the second one is a code defect; both must be
recorded, because fixing only the code leaves the reported symptom intact.

**Fault 1 — host permissions (environment, not in this repository).**
The Compose bind source `MCP_HOST_SHARED_DIR` (default `./mcp-files`) is written
by the container's non-root `app` user, UID 10001 (`Dockerfile`). On the
reporting host the directory had been left at `1000:1000` mode `0755`:

```
$ docker compose exec -T mcp python -c \
    "import os;print(os.access('/mcp-files/output', os.W_OK))"
False
```

`_copy_output` reaches `os.open(<temp>, os.O_CREAT | os.O_EXCL | ...,
dir_fd=directory_fd)` and receives `PermissionError`. `docs/ops.md` and the
README already prescribe granting UID 10001 write access to the dedicated host
directory; the failure was a setup-sequencing mistake (`chmod 0777` applied to a
directory that was later removed and recreated), not a documentation gap. Owner
action, outside the repository:

```bash
sudo chown -R 10001:10001 mcp-files
sudo chmod 0755 mcp-files/output
```

**Fault 2 — error misclassification (code defect, fixed here).**
`McpTools.download_result` in `app/mcp_server/server.py` collapses the whole
filesystem step into one clause:

```python
except (OSError, ValueError):
    return safe_error(ErrorCode.INVALID_REQUEST)
```

`_copy_output` raises `ValueError` for the deliberate containment guards
(symlinked destination, replaced temporary file, inode mismatch) — for those,
`invalid_request` is correct and already covered by tests. It also raises
`OSError` for conditions the caller cannot fix by changing its arguments:
unwritable destination directory, `ENOSPC`, `EIO`, read-only mount. Those are
reported as `invalid_request` / "Request validation failed" / `retryable: false`,
which (a) blames the client's arguments, (b) denies retry although the catalog
marks `internal_error` retryable, and (c) hides the actual cause from every
operator reading the error payload — the exact reason this defect survived a
successful-looking translation run.

---

## Invariants this plan must not break

1. **No new error codes.** Both `INVALID_REQUEST` and `INTERNAL_ERROR` already
   exist in `app/core/errors.py`; only the mapping between an exception type and
   an existing catalog entry changes.
2. **No exception text leaves the process.** No `str(exc)`, errno, path or
   traceback in the tool result or in logs (AGENTS.md rule 6). `safe_error`
   keeps supplying the catalog message verbatim.
3. **Containment is unchanged.** No edit to `_copy_output`, `_validate_destination`,
   `open_shared_directory`, or `resolve_shared_path`. Every symlink/traversal
   rejection must still surface as `INVALID_REQUEST`.
4. **Layering unchanged.** `app/mcp_server/` gains no SQL, no business rules and
   no new dependency on `api/` or `worker/` (rule 3).
5. **Async discipline unchanged.** The copy still runs inside `asyncio.to_thread`;
   no blocking call enters the event loop (rule 9).
6. **No schema change, no port change, no model change.** `DownloadResult`,
   `ToolError` and `JobSummary` are untouched.

---

## Current state

- `app/mcp_server/server.py` — three sibling `except (OSError, ValueError)`
  clauses: line 101 (`translate_file`, correct there: bad path *is* an invalid
  request), line 148 (`storage.get_output_path`, already narrowed to
  `INTERNAL_ERROR` for the same class of fault), line 156 (`download_result`,
  the defect).
- Line 148 is the in-repo precedent for this fix: artifact-lookup I/O failures
  already map to `INTERNAL_ERROR`, so line 156 is inconsistent with its sibling.
- `tests/mcp_server/test_job_tools.py` — `test_download_complete_partial_and_path_guards`
  covers the `ValueError` boundary; no test covers the `OSError` boundary.
- `tests/mcp_server/test_server.py` — protocol-level test calling
  `download_result` with `{"job_id", "output_dir"}`; must stay green.
- `mcp-files/` is untracked and not listed in `.gitignore`, so host input PDFs
  and downloaded artifacts are one `git add -A` away from being committed.
- Working tree carries unrelated user edits (`PROMPTS.md`, `docs/assessment_context/roadmap.md`,
  two `docs/plans/*` files, untracked `OVERVIEW.md`, `docs/plans/2026-10-03-benchmark-matrix.md`,
  `docs/plans/2026-10-03-docx-tables-design.md`). They must stay excluded from
  this change's staging.

---

## Phase 1 — Red test (TDD)

Add `test_download_maps_filesystem_errors_to_internal_error` to
`tests/mcp_server/test_job_tools.py`.

Setup reuses the existing fixtures `runtime` and `input_pdf`, drives a job to a
terminal state exactly as `test_download_complete_partial_and_path_guards` does
(`runtime.storage.save_output(...)` then `repository.complete_job(..., JobStatus.COMPLETED_WITH_ERRORS)`),
then stubs the copy step:

```python
def _boom(exc: Exception) -> Callable[..., Path]:
    def _raise(*_args: object, **_kwargs: object) -> Path:
        raise exc

    return _raise
```

Assertions:

| Stub raises | Expected `error_code` | Expected `retryable` | Expected `message` |
|---|---|---|---|
| `PermissionError("…")` | `ErrorCode.INTERNAL_ERROR` | `True` | `"Internal server error"` |
| `OSError(errno.ENOSPC, "…")` | `ErrorCode.INTERNAL_ERROR` | `True` | `"Internal server error"` |
| `ValueError("…")` | `ErrorCode.INVALID_REQUEST` | `False` | `"Request validation failed"` |

Stubbing `_copy_output` through `monkeypatch.setattr("app.mcp_server.server._copy_output", …)`
is required, not optional: a `chmod 0555` based test would silently pass when the
suite runs as root, and would make the test depend on uid.

The stubbed exception carries realistic text, and the assertion on `message`
proves that text is not propagated — invariant 2 is tested, not just asserted in
prose.

---

## Phase 2 — Minimal fix

`app/mcp_server/server.py`, `download_result`, line 156:

```python
except ValueError:
    return safe_error(ErrorCode.INVALID_REQUEST)
except OSError:
    return safe_error(ErrorCode.INTERNAL_ERROR)
```

Ordering matters only in that both are disjoint types; no behavior depends on
which is listed first. Nothing else in the file changes — lines 101 and 148 are
already correct for their own contracts.

Rejected alternatives:

- **Broadening the catalog** with a `storage_unavailable` code. Rejected: it is a
  public contract addition requiring approval under AGENTS.md rule 4, and
  `internal_error` already carries the correct `retryable: true` semantics.
- **Returning `INVALID_REQUEST` with a better message.** Rejected: a per-tool
  message override bypasses the single catalog source of truth and still tells
  the client its request was malformed.
- **Pre-checking destination writability** with `os.access` before copying.
  Rejected: TOCTOU, and it would duplicate the containment logic.

---

## Phase 3 — Repository hygiene

1. Append `mcp-files/` to `.gitignore` (data & runtime artifacts block). One
   line; prevents committing operator documents and download artifacts.
2. Optional, requires user sign-off: add one row to the *Failure handling matrix*
   (`ARCHITECTURE.md`) — "MCP download destination unwritable → `internal_error`,
   a server-side fault, not a client argument problem". The matrix currently has
   no MCP row at all. Per the definition of done, behavior changes should be
   documented; per the user's scope decision, no README/`docs/ops.md` edit.

---

## Phase 4 — Validation matrix (real MCP client)

Unit tests prove the mapping. They cannot prove the *reported* symptom is gone,
so the matrix runs the actual Codex CLI against the running Compose stack, using
the already-`done` job `ff7155aa-0986-48c7-a129-39806498d3b4`
(`/data/out/ff7155aa-…/sample_en.pdf`, 1 708 460 bytes, 1/1 chunk, $0.00041655).
Only Codex tokens cost anything: `LLM_PROVIDER=fake`, so no translation is billed.

Shared invocation shape (one flag differs per run):

```bash
codex exec --ephemeral -C ~/projects/POCs/document-translator -s read-only \
  -c model_reasoning_effort="low" --approve-for-me -o /tmp/codex-v<N>.txt \
  'Используй только MCP-инструменты stark-translate. Вызови download_result
   с job_id="ff7155aa-0986-48c7-a129-39806498d3b4" и output_dir="output".
   Покажи ответ сервера дословно, ничего не повторяй.'
```

`--approve-for-me` is required: `~/.codex/config.toml` sets
`approval_mode = "approve"` on the three tools, and the reproduction from the
field report shows an interactive approval prompt (`mkdir -p output`) mid-run.
`-s read-only` is sufficient because the client needs no shell writes.

| # | State | Expected server payload | Gate |
|---|---|---|---|
| **V0** | current code, host permissions broken | `invalid_request` / `Request validation failed` / `retryable: false` | before Phase 2 |
| **V1** | fixed code, permissions still broken | `internal_error` / `Internal server error` / `retryable: true` | after Phase 2 + rebuild |
| **V2** | fixed code, permissions granted | `{"job_id": "ff7155aa-…", "path": "…"}` | owner runs the two `sudo` lines |

V0 must be captured **before** the code edit — it is the only proof that the
reproduction matches the field report. V0 and V1 together are the A/B that
isolates Fault 2 from Fault 1. V2 requires the owner's `sudo` (no password
available to the agent) and closes Fault 1.

Side-effect checks, all read-only from the agent:

- `docker compose exec -T mcp python -c "import os;print(os.access('/mcp-files/output',os.W_OK))"`
  → `False` before V2, `True` after.
- After V2: the artifact exists on the host as
  `mcp-files/output/<sha256(job_id)[:16]>-sample_en.pdf`, mode `0644`, readable
  by the host user — confirming the DT-65 guarantee still holds for a
  container-created file.

Rebuild between V0 and V1: `docker compose up -d --build mcp`. V0 must not run
against a rebuilt image.

Out of matrix by decision: a fresh full-cycle Codex run (translate → poll → download).
It would cost Codex tokens while adding no signal about this defect.

---

## Phase 5 — Review

**Automated.** `codex review --uncommitted` with an explicit scope, because the
tree also holds unrelated user edits that would otherwise dominate the review:

```bash
codex review --uncommitted \
  'Review ONLY the uncommitted changes in app/mcp_server/server.py,
   .gitignore and tests/mcp_server/test_job_tools.py. Ignore docs/,
   OVERVIEW.md and mcp-files/. Check: error-mapping correctness (ValueError vs
   OSError), whether any existing symlink/traversal guard now returns the wrong
   code, whether any exception text can leak, and test quality.'
```

**Manual checklist** against AGENTS.md before showing the diff to the owner:

- Rule 6 — only codes from `app/core/errors.py`; `str(exc)` reaches neither the
  result nor a log line.
- Rule 3 — no SQL, no business logic, no new cross-layer import in `mcp_server/`.
- Rule 9 — copy still inside `asyncio.to_thread`; no blocking call added.
- `NOT_FOUND` and `CONFLICT` are still decided above `_copy_output`, so the new
  `OSError` clause cannot mask them.
- The new test is uid-independent and runs as root.
- `tests/mcp_server/test_server.py` (protocol level) unaffected.

---

## Tests

| Test | Type | Asserts |
|---|---|---|
| `test_download_maps_filesystem_errors_to_internal_error` | new, unit | `OSError` → `internal_error`/retryable; `ValueError` → `invalid_request`; catalog message verbatim |
| `test_download_complete_partial_and_path_guards` | existing, unit | containment guards still `invalid_request` (invariant 3) |
| `test_server.py::…download_result…` | existing, protocol | tool contract and JSON shape unchanged |
| V0/V1/V2 | manual, live client | the field-reported symptom is reproduced, then reclassified, then resolved |

Suites: `make test`, `make lint`, `make typecheck`. No `@pytest.mark.live` test
is added; the LLM boundary is untouched.

---

## Out of scope

- Host permission setup itself — owner's two `sudo` lines, already documented.
- Any change to `_copy_output`, path resolution or containment.
- A new error code or a new catalog message.
- README / `docs/ops.md` rewording (explicit user decision: the existing
  `chmod 0777` guidance was followed correctly; the application order was not).
- `mcp-files/` bind-mount ownership as a Compose concern (e.g. `user:` override),
  which would change the non-root image guarantee delivered under DT-58/DT-65.
- The remaining untracked/planned documents in the working tree
  (`OVERVIEW.md`, benchmark matrix, DOCX tables design).

---

## Execution notes

- Ticket `DT-90`; expected commit message
  `DT-90: fix(mcp): map download filesystem errors to internal_error`.
- **No commit without an explicit request.** `TASKS.md` is updated (new table row
  plus a stage-11 execution paragraph) and left uncommitted alongside the code,
  matching how the user has been driving delivery.
- Unrelated user edits stay unstaged; stage explicitly by path.
- If Phase 4 V1 does not return `internal_error`, the assumption that Fault 2 is
  the only code-side cause is wrong — stop and re-enter root-cause analysis
  rather than adding a second mapping change.
- If V2 still fails after the `chown`, do not touch the mapping: re-check
  `os.access` and whether Compose picked up a different
  `MCP_HOST_SHARED_DIR` from the environment.