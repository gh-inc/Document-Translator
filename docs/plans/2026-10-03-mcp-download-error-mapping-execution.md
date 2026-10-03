# DT-90 execution record

Plan: [MCP download error mapping](2026-10-03-mcp-download-error-mapping.md).

## Ownership and decomposition

- Runtime agent: capture V0 before edits, rebuild and capture V1, inspect V2 prerequisites.
- Implementation agent: uid-independent red test, minimal exception split, ignore runtime artifacts.
- Independent reviewer: mapping, containment, safe messages and test quality.
- Root: task tracking, full verification, evidence and scoped commits.

Existing feature branch `markdown-format` and unrelated user changes are preserved.
No new public contracts, dependencies or provider calls. The architecture sections
“Layering & the Document IR”, “MCP server”, “Failure handling matrix” and
“Testing strategy” govern the change.

Ruling: keep this existing checkout for the running Compose reproduction; explicit
path staging isolates delivery. No merge, push or history rewriting.
Ruling: omit the optional ARCHITECTURE matrix edit because the plan requires
separate sign-off; document behavior here instead.
Ruling: V2 ownership changes remain the owner’s action, as scoped by the plan.

## Progress

Repository implementation, verification and independent review complete.
External V2 and authenticated Codex CLI acceptance remain explicitly pending.

## Regression and behavior

The regression stubs `_copy_output`, independent of uid. Before the fix:
2 failed (PermissionError and ENOSPC), 1 passed (ValueError). After the fix:
3 passed; focused tools/protocol suite: 15 passed. The copy and containment
helpers, schemas, catalog, provider boundary and `asyncio.to_thread` are unchanged.
Filesystem failures now return catalog `internal_error` / `Internal server error` /
`retryable: true`; validation guards retain `invalid_request` /
`Request validation failed` / `retryable: false`. No exception diagnostics escape.
`mcp-files/` is now ignored. The plan's Python example received one blank line
required by the repository's Markdown code-block formatter.

## Runtime matrix

The stack was initially stopped. The runtime agent started the existing image
without rebuilding before V0, then rebuilt after the code fix for V1.
Both checks used the existing completed job `ff7155aa-0986-48c7-a129-39806498d3b4`.

| Check | Destination writable | Actual structured result | Outcome |
|---|---|---|---|
| V0, original image | false | `invalid_request`, `Request validation failed`, false | reproduced |
| V1, rebuilt image | false | `internal_error`, `Internal server error`, true | passed |
| V2, owner permission correction | not yet performed | not captured | pending owner |

Ruling: use the installed FastMCP client over real streamable HTTP inside the
container for V0/V1 after Codex CLI authentication failed. This verifies the
server mapping, but does not establish acceptance through authenticated Codex CLI.
The CLI reported `Not logged in` and HTTP 401 before any MCP call. Its installed
version also rejects combining `--sandbox read-only` with `--approve-for-me`.
No fresh translation or provider call was made. FastMCP checks used zero model
tokens. The running rebuilt stack is retained.

V2 remains outside repository scope: owner must run the two documented sudo
commands, after which download success, mode 0644 and host readability can be
checked. No ownership or permission mutation was performed by agents.

## Review and checks

Independent scoped review found no actionable issues; `git diff --check` passed.
The review verified NOT_FOUND/CONFLICT precedence, existing path guard mappings,
layering, catalog messages, async copying and unchanged tool contracts.
The requested `codex review --uncommitted` with a scope prompt is rejected by
this CLI (`--uncommitted` cannot be combined with a prompt, including stdin).
An unscoped review of unrelated user edits was not substituted.

Initial full-suite run had a failure in the pre-existing concurrent legacy
SQLite startup test at `PRAGMA journal_mode=WAL`; all MCP tests passed. The
failed run remained alive due to a connection left by the failing concurrent
test and was interrupted after reporting results. An intermediate rerun without retained output was interrupted; no result was
claimed. The final `make test` run retained output in `/tmp/dt90-full-test.log`
and exited 0: **579 passed, 2 live tests deselected, 2 existing warnings**,
61.99 seconds. The isolated SQLite file also passed: 17 tests.
Lint passed (162 files formatted); mypy passed (60 source files).


## Token accounting

Snapshot taken after checks, before delivery commits: 10,244,249
input tokens (including 10,016,411 cached input),
35,160 output tokens (including reported reasoning).
These are the sum of the latest `total_token_usage` events for this root session
and its three delegated session files, not an estimate of document translation
usage or a provider invoice. Remaining commit/report turns are not included.
No successful nested Codex model call occurred; FastMCP verification used 0/0.

## Delivery

Implementation and evidence commit: `7f2c224`. Task hash is backfilled in a
separate documentation commit, without rewriting history. Unrelated edits
remain unstaged.
