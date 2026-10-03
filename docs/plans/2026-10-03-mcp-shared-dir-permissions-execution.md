# DT-101 shared-directory permissions acceptance (2026-10-04)

Source: [approved plan](2026-10-03-mcp-shared-dir-permissions.md).
Architecture references: MCP surface, Testing strategy, Observability.

## Decomposition and ownership

1. Existing phases 1–4 inspected: commits 3aefe66, 04d96ac, f379293,
   dd3bc2c, 41a68fe. No duplicate implementation.
2. Doctor worker owns script and its regression tests.
3. Independent reviewer owns startup preflight fixes and tests after review.
4. Acceptance worker owns disposable Docker/MCP transport evidence only.
5. Root owns integration, documentation, task log, full checks and commit.

## Decisions

- Work in the current feature checkout; preserve unrelated untracked plan.
- Missing output is healthy when the shared parent permits creation. The plan's
  literal W_OK-on-missing-path example would produce a false startup error.
- Strengthen read-only preflight and doctor checks for actual directory access;
  containment, copy and publication implementation remain unchanged.
- Keep the plan-mandated PermissionError mapping unchanged. Review identified
  that a source-file PermissionError inside the copy also maps to the shared-dir
  error; distinguishing it requires changing the explicitly protected copy
  boundary and remains a documented limitation.
- Test ownership repair in disposable fixtures, not the operator's live share;
  use FakeProvider and real MCP transport without paid provider calls.

## Verification

- Runtime/MCP regressions: 43 passed; doctor regressions: 11 passed.
- Red/green doctor evidence: added mode/access/quoting regressions failed against
  the original script and passed after implementation.
- Independent cross-review completed. Logged remedy now preserves artifacts;
  access-mask assertions pin root RX and output/creation-parent RWX.
- `make lint`: clean, 181 files formatted; `make typecheck`: 61 source files clean.
- Full `make test`: **706 passed, 2 live tests deselected**, 6 warnings. Sandbox runs stalled on I/O and were stopped;
  final full run uses approved execution outside sandbox.
- `make mcp-share-check`: correctly exits non-zero for the operator's existing
  output directory (host-visible uid 65534, mode 0755 versus service uid 10001).
  Existing host artifacts and ownership were preserved.
- Real FastMCP streamable-HTTP acceptance passed with isolated uid10001 MCP and
  worker containers, current source mounted read-only, and FakeProvider only.
  One Markdown chunk completed; initial download returned
  `shared_dir_unavailable`, `retryable=false`; after removing only the disposable
  fixture output directory, download succeeded with mode 0644 and host readability.
  Exactly one startup event was emitted. No live provider calls.
  Evidence: `/tmp/dt101/acceptance-report.md` and
  `/tmp/dt101-run-20261003T235341Z-1515237/`.
- Containment helpers, copy, atomic publication, Dockerfile and Compose unchanged.
- No deployed Compose rebuild or real editor client run: the transport test used
  the installed FastMCP Client and existing image with current source mounted.
  PDF/DOCX fidelity was not re-measured in this one-chunk Markdown smoke.

## Plan deviations and remaining operational work

The doctor requires input read/search, not input write; it checks the permissions
actually needed by the service. It conservatively rejects symlink directories;
contained symlinks can still work in the service. ACLs, supplementary groups and
ancestors above the shared root remain outside the numeric-mode doctor estimate.
The documented `chmod 0777` advice was explicitly replaced with ownership-first
instructions. The live host needs an operator ownership fix; deleting output would
remove existing downloads and is not performed by this task.

## Agent token accounting

Local `token_count` records across root and three delegated sessions at the
pre-delivery checkpoint: input **11,643,345**, output **76,034**.
Input includes repeated cached context; this is token usage, not a billing quote.
Raw per-session checkpoint: `/tmp/dt101-token-checkpoint.json`.
Later verification, commit and report turns are excluded from this checkpoint.


Delivery commit: `d464c8d`. Final recorded checkpoint before hash-backfill commit:
input **12,166,164**, output **78,266**, cached input **11,841,447**.
Excludes the hash-backfill commit/report turn after this snapshot.
