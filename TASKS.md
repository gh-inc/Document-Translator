# TASKS.md — Backlog & Task Log

Ticket source of truth for commit messages (see
[CONTRIBUTING.md](./CONTRIBUTING.md)). Prefix: **DT**. Numbers are
sequential and never reused, even if a task is cancelled.

## How agents use this file

1. Pick the next `todo` task — or, before starting a stage, decompose it
   into tasks numbered `DT-<next sequential>` and list them here first.
2. Mark the task `in-progress` when you start; reference it in every commit
   (`DT-<N>: <type>(<scope>): ...`).
3. When finished, mark `done` and backfill the commit hash(es).
4. One task = one logical, reviewable change. If a task grows, split it —
   never renumber; new tasks get new numbers.

Statuses: `todo` → `in-progress` → `done` (or `cancelled`, with reason).

## Stages (implementation order, per ARCHITECTURE.md)

0. Repository conventions & docs
1. Persistence layer (schema.sql, repositories, WAL pragmas)
2. LLM port + FakeProvider + OpenAIProvider
3. Format adapter: PDF (extractor + renderer)
4. Worker: leases, claim loop, bounded-parallel executor, retry/backoff
5. REST API + SSE
6. Format adapter: DOCX (proves the format port)
7. Triage agent (openai-agents SDK) + degraded fallback
8. MCP server
9. Frontend (React, Stark branding)
10. Chaos script + measurements for DECISIONS.md

## Tasks

| ID | Stage | Title | Status | Commits |
|----|-------|-------|--------|---------|
| DT-1 | 0 | Establish git conventions and task backlog | done | this change |
