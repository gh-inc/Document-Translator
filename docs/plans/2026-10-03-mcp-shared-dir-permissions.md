# MCP shared-directory permissions — implementation plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the MCP download path either work or fail with one actionable
operator-facing line, instead of an anonymous `internal_error` that clients retry
forever.

**Architecture:** No change to the containment model, the copy, or the atomic
publication. The remedy is operational — host directory ownership — because the
code already self-provisions correct ownership when the directory is missing. The
code change is about the *legibility* of the failure, not about hiding it.

**Tech Stack:** Python 3.12, FastMCP, Compose, bash, pytest. No new dependency, no
schema change.

**Ticket:** **DT-101** (follows DT-100).

---

## Origin

Observed through a real Codex MCP client, job `ff7155aa…` `done`, 1/1 chunk,
artifact present at `/data/out/ff7155aa…/sample_en.pdf`:

```
Called stark-translate.download_result
  └ {"error_code":"internal_error","message":"Internal server error",
     "retryable":true,"document_id":null}
```

The client, told the fault was retryable, retried indefinitely.

### The finding that shapes the remedy

`McpRuntime.startup` runs `mkdir(parents=True, exist_ok=True)`, and
`open_shared_directory(..., create=True)` repeats `os.mkdir(component, 0o755,
dir_fd=...)` with `FileExistsError` suppressed (`app/mcp_server/paths.py`). **If the
directory did not exist, the service would create it under its own uid 10001 and
everything would work.**

Measured on the reporting host:

```
/mcp-files           uid=10001  0755  R_OK=True   W_OK=True
/mcp-files/input     uid=1000   0755  R_OK=True   W_OK=False   (read-only for the service)
/mcp-files/output    uid=1000   0755  R_OK=True   W_OK=False   ← the fault
```

`mcp-files/` itself is owned by the service uid because the image created it before
the bind mount. The two subdirectories are host-owned, so `exist_ok=True` passes
straight through a misconfigured mount.

Three defects stack:

| | Defect | Where |
|---|---|---|
| **F1** | Host-owned `output` is unwritable by the service uid | environment |
| **F2** | Nothing verifies usability at startup, so the fault surfaces only at first use | `runtime.py:72-80` |
| **F3** | `internal_error` is `retryable=true`, so clients retry a fault no client action can fix | `server.py:156` |

F3 is a direct consequence of DT-90, and worth naming: that change made the
diagnosis honest and the client's behaviour worse. Before it, the client failed a
few times and stopped; now it loops.

F2 is the expensive one. The only evidence available was a `PermissionError` buried
in a traceback inside a container.

## Invariants this plan must not break

1. **Containment unchanged.** No edit to `paths.py`, `resolve_shared_path`,
   `open_shared_directory`, `_copy_output`, or `_validate_destination`. The
   `O_NOFOLLOW` / directory-descriptor guarantees stay exactly as they are.
2. **No leakage.** No directory path, listing, or document text in any new log
   line or error payload.
3. **The service never repairs host ownership.** No `chown`, no `chmod`, no
   directory replacement from inside the container.
4. **Artifacts stay mode `0644`** so the host user can read container-created files
   (DT-65).
5. **Only permission faults are reclassified.** `ENOSPC`, `EIO` and every other
   `OSError` keep `INTERNAL_ERROR` / `retryable=true`.
6. **The service keeps running as non-root UID 10001** (DT-58, DT-65). No
   `user:` override in Compose.

---

## Phase 1 — Preflight the shared directory at startup

**Files:**
- Modify: `app/mcp_server/runtime.py` (`McpRuntime.startup`)
- Test: `tests/mcp_server/test_server.py` or the nearest runtime test module —
  check with `ls tests/mcp_server` before adding a file

**Step 1 — write the failing test**

```python
async def test_startup_warns_when_shared_output_is_unwritable(
    mcp_settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    runtime = McpRuntime(mcp_settings)
    monkeypatch.setattr(os, "access", lambda *_args, **_kwargs: False)
    try:
        with caplog.at_level(logging.ERROR):
            await runtime.startup()
    finally:
        await runtime.aclose()
    record = next(r for r in caplog.records if r.event == "mcp_shared_dir_not_writable")
    assert record.kwargs["uid"] == os.getuid()
    assert "remedy" in record.kwargs
```

Probe by monkeypatching `os.access`, **not** by `chmod`-ing a fixture directory: a
permission-based test silently passes when the suite runs as root, which is
exactly the environment where this bug would be invisible.

**Step 2 — run it and watch it fail**

```bash
uv run pytest tests/mcp_server -q -k unwritable
```

Expected: no `mcp_shared_dir_not_writable` record is emitted.

**Step 3 — implement**

```python
async def startup(self) -> None:
    """Initialize WAL and report an unusable shared directory before serving."""
    for directory in (
        self.settings.mcp_shared_dir,
        self.settings.upload_storage_path,
        self.settings.output_storage_path,
    ):
        await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
    await self._report_shared_directory()
    connection = await self.factory.create()
    await _finish_cleanup(connection.close())


async def _report_shared_directory(self) -> None:
    """Log one actionable line when a download cannot be written.

    Reads still work, so this must never stop the server: `translate_file`
    and `check_status` stay usable and only `download_result` is affected.
    The fix is host-side ownership; the service never changes host
    permissions itself. Only the child directory name is logged, never a
    resolved host path.
    """
    probe = self.settings.mcp_shared_dir / "output"
    writable = await asyncio.to_thread(os.access, probe, os.W_OK)
    if not writable:
        logger.error(
            "mcp_shared_dir_not_writable",
            uid=os.getuid(),
            directory_name=probe.name,
            remedy=(
                "fix host ownership of MCP_HOST_SHARED_DIR for the service uid; "
                "the service creates this directory itself when it is missing"
            ),
        )
```

Add `import os` to the module. No new module-level import of `logging` is needed;
`caplog.at_level` in the test handles that.

**Step 4 — run the MCP suite**

```bash
uv run pytest tests/mcp_server -q
```

Expected: all pass. Also confirm the positive case emits nothing: add

```python
async def test_startup_is_silent_when_shared_output_is_writable(...) -> None:
    ...
    assert not [r for r in caplog.records if r.event == "mcp_shared_dir_not_writable"]
```

**Commit only when the owner asks:**
`DT-101: feat(mcp): report an unusable shared directory at startup`

---

## Phase 2 — Make the permanent fault non-retryable

`PROVIDER_AUTH_ERROR` is already `retryable=false` because only a human can fix
credentials. An unwritable host directory is the same class of fault: no client
action resolves it.

**Files:**
- Modify: `app/core/errors.py` (`ErrorCode`, `_CATALOG`)
- Modify: `app/mcp_server/server.py` (`download_result`)
- Test: `tests/mcp_server/test_job_tools.py`

**Step 1 — write the failing test**

```python
async def test_download_reports_unwritable_share_as_non_retryable(
    runtime: McpRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = McpTools(runtime)
    submission = await tools.translate_file(str(input_pdf), ["de"])
    job_id = submission.job_ids[0]

    def _deny(*_args: object, **_kwargs: object) -> Path:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("app.mcp_server.server._copy_output", _deny)
    result = await tools.download_result(job_id, "output")

    assert isinstance(result, ToolError)
    assert result.error_code is ErrorCode.SHARED_DIR_UNAVAILABLE
    assert result.retryable is False
```

Follow the module's existing fixture style — `tests/mcp_server/test_job_tools.py`
already builds a completed job via `runtime.storage.save_output` and
`repository.complete_job`; reuse that shape rather than inventing a new harness.

**Step 2 — run it and watch it fail**

```bash
uv run pytest tests/mcp_server/test_job_tools.py -q -k unwritable_share
```

Expected: `internal_error` instead of the new code.

**Step 3 — extend the catalog**

`app/core/errors.py`, in `ErrorCode`:

```python
    SHARED_DIR_UNAVAILABLE = "shared_dir_unavailable"
```

and in `_CATALOG`:

```python
    ErrorCode.SHARED_DIR_UNAVAILABLE: (
        "Shared translation directory is not writable by the service",
        False,
    ),
```

The message describes the condition without naming a host path, and `retryable`
is `False` so an MCP client stops instead of looping.

**Step 4 — map `PermissionError` before the general `OSError`**

`app/mcp_server/server.py`, in `download_result`:

```python
        except PermissionError:
            return safe_error(ErrorCode.SHARED_DIR_UNAVAILABLE)
        except OSError:
            return safe_error(ErrorCode.INTERNAL_ERROR)
```

Order is what matters: `PermissionError` is an `OSError` subclass, so the general
clause would swallow it. Nothing else in the method moves.

**Step 5 — keep the existing boundaries covered**

Assert in the same test module that a generic `OSError` still yields
`INTERNAL_ERROR` / `retryable=True`, so invariant 5 is tested rather than asserted
in prose.

**Step 6 — run**

```bash
uv run pytest tests/mcp_server -q
```

**Commit only when the owner asks:**
`DT-101: fix(mcp): stop client retry loops on an unwritable shared directory`

---

## Phase 3 — A read-only doctor instead of a privileged helper

The repository must not ship something that calls `sudo`. A checker that reports
state and prints the exact remedy keeps the privilege decision with the operator.

**Files:**
- Create: `scripts/prepare-mcp-share.sh`
- Modify: `Makefile`
- Test: `tests/scripts/` — follow the existing convention there; the script must be
  runnable without Docker

**Step 1 — write the failing test**

```python
def test_doctor_script_reports_unwritable_directory(tmp_path: Path) -> None:
    shared = tmp_path / "mcp-files"
    (shared / "output").mkdir(parents=True)
    result = subprocess.run(
        [str(SCRIPT), str(shared)],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "SERVICE_UID": "10001"},
    )
    assert result.returncode == 1
    assert "sudo rm -rf" in result.stdout
```

The script must accept the directory as an argument so the test needs no root, no
Docker and no real host mount.

**Step 2 — write the script**

```bash
#!/usr/bin/env bash
# Report whether the MCP shared directory is usable by the container.
# Read-only: it never elevates privilege and never modifies anything.
set -Eeuo pipefail

usage() {
    printf 'Usage: %s [shared-directory]\n' "${0##*/}" >&2
    printf '  Reports ownership and writability for the service uid (SERVICE_UID, default 10001).\n' >&2
}

case "${1:-}" in
    -h|--help) usage; exit 0 ;;
esac

repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
shared_dir="${1:-$repo_dir/mcp-files}"
service_uid="${SERVICE_UID:-10001}"
```

For each of `input` and `output`: report presence, uid, mode and whether the
service uid can write. On an `output` directory the service cannot write, print the
remedy and exit `1`:

```
mcp-files/output is owned by uid 1000, mode 0755; the service runs as uid 10001.
The service creates this directory itself when it is missing, so the simplest fix is:
  sudo rm -rf mcp-files/output
```

Exit `0` only when every probed directory is usable. Never `chown`, never `chmod`,
never `rm`.

**Step 3 — add the Makefile target**

```make
mcp-share-check:
	./scripts/prepare-mcp-share.sh
```

Add `.PHONY`. Do not wire it into `test` or any build target: CI has no business
asserting host permissions.

**Step 4 — make it executable and run it**

```bash
chmod +x scripts/prepare-mcp-share.sh
make mcp-share-check   # exits 1 today, printing the remedy
uv run pytest tests/scripts -q
```

**Commit only when the owner asks:**
`DT-101: chore(scripts): add a read-only MCP shared-directory check`

---

## Phase 4 — Correct the documented guidance

`README.md` (the "Three-step MCP verification" section) and `docs/ops.md` both
prescribe `chmod 0777 mcp-files/output`. World-writable is unnecessary: mode `0755`
owned by the service uid suffices, because artifacts publish at `0644` and the host
only needs read (DT-65).

**Files:**
- Modify: `README.md`, `docs/ops.md`

**Step 1 — replace the `chmod 0777` advice**

State the ownership-first sequence, then the self-healing alternative:

```bash
sudo chown -R 10001:10001 mcp-files
```

or, when only `output` is wrong, let the service provision it:

```bash
sudo rm -rf mcp-files/output
```

**Step 2 — name the affected tool**

State plainly that a wrong-owned output directory affects `download_result` only;
`translate_file` and `check_status` keep working because they only read.

**Step 3 — reference the checker**

Point operators at `make mcp-share-check`, and describe the startup line
`mcp_shared_dir_not_writable` as the thing they will see if it is still wrong.

**Step 4 — note the non-retryable error**

Record that `download_result` answers `shared_dir_unavailable` with
`retryable=false`, so a client stops rather than retrying, and that a host-side
ownership fix is the remedy.

**Commit only when the owner asks:**
`DT-101: docs(mcp): correct the shared-directory ownership guidance`

---

## Phase 5 — Verify

**Step 1 — before and after the remedy**

```bash
make mcp-share-check; echo "exit=$?"
sudo rm -rf mcp-files/output
make mcp-share-check; echo "exit=$?"
```

Expected: `exit=1` with the `sudo rm -rf` remedy, then `exit=0`.

**Step 2 — startup is clean after the fix**

```bash
docker compose up -d --build mcp
docker compose logs mcp | grep -c mcp_shared_dir_not_writable   # 0
```

**Step 3 — end-to-end through the real client**

`translate_file` → poll `check_status` → `download_result`, then confirm on the host:

```bash
ls -l mcp-files/output
```

The artifact must exist at mode `0644` and be readable by the host user.

**Step 4 — regression: the fault is non-retryable and singular**

With the directory deliberately made unwritable again, one `download_result` call
must answer `shared_dir_unavailable` with `retryable=false`, and the client must not
loop.

**Step 5 — full suites**

```bash
make test && make lint && make typecheck
```

---

## Verification matrix

| Requirement | Test | Status |
|---|---|---|
| Startup reports an unwritable shared `output` | `tests/mcp_server` | new |
| Startup stays silent when the directory is usable | `tests/mcp_server` | new |
| No host path or listing in the log line | same | new |
| `download_result` → `shared_dir_unavailable`, `retryable=false` | `tests/mcp_server/test_job_tools.py` | new |
| A generic `OSError` still → `internal_error`, retryable | same | new |
| Value errors still → `invalid_request` | existing path-guard test | existing |
| Doctor script detects a wrong-owned directory and prints the remedy | `tests/scripts` | new |
| Doctor script never mutates anything | same | new |
| `make mcp-share-check` reflects reality | manual | manual |
| Containment unchanged | existing `test_paths.py`, `test_job_tools.py` | existing |
| Artifacts still `0644` | `test_copy_output_is_private_until_complete_and_published_readable` | existing |
| Service still non-root | Dockerfile + Compose unchanged | existing |

## Out of scope

- Running the MCP service as the host uid via a Compose `user:` override. That
  undoes the non-root image guarantee delivered under DT-58 and DT-65.
- Any `chmod`, `chown` or directory replacement from inside the container.
- Changing the containment model, the copy, the atomic publication, or the
  destination filename format.
- Classifying `ENOSPC`, `EIO` or any other `OSError`.
- Changing HTTP status codes or the REST error surface. This code is MCP-only and
  must not appear in an HTTP response body.
- The worker's "schema wider than code" startup guard — same shape, separate task.

## Execution notes

- **Never commit without an explicit request.**
- The immediate unblock requires no code at all: `sudo rm -rf mcp-files/output`,
  then retry `download_result`. The directory is recreated by the service on first
  use, without a restart.
- Phase 2 is an additive public-contract change, approved by the owner for this
  ticket. It touches only the MCP tool result, never an HTTP response.
- Phase 4 contradicts a previously documented instruction (`chmod 0777`). Say so
  in the delivery summary rather than silently replacing it.
- Do not "fix" a future recurrence by loosening the destination check. The failure
  is real, and containment must keep rejecting everything else.