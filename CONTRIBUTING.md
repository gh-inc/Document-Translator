# Git Workflow & Commit Guidelines

We enforce a strict commit message format based on the **Conventional
Commits** specification, aligned with our issue tracking.

> **This repository:** `PROJECT` = **DT** (Document Translator). Ticket
> numbers come from the repo-local backlog [`TASKS.md`](./TASKS.md), which
> plays the role of the issue tracker for this project.

## Commit Message Format

Every commit message must strictly match the following pattern:

`<PROJECT>-<TICKET_NUMBER>: <type>(<scope>): <short description>`

* **PROJECT**: The project abbreviation in uppercase (`DT`).
* **TICKET_NUMBER**: The sequential task number from the backlog in
  `TASKS.md` (e.g., `124`).
* **type**: The type of changes being made (`feat`, `fix`, `refactor`,
  `docs`, `style`, `test`, `chore`).
* **scope** *(optional)*: The specific module or area affected (e.g.,
  `worker`, `api`).
* **short description**: A brief summary of the changes in the imperative
  mood (e.g., "add login" instead of "added login").

### Valid Examples:
* `DT-124: feat(worker): add chunk lease reclaim on startup`
* `DT-45: fix(api): resolve crash on empty upload`
* `DT-812: refactor: optimize database queries`

---

## Clean History Rules

1. **Local Development:** While working in your local branch, feel free to
   make intermediate temporary commits (`wip`, `fix typo`, `tmp`).
2. **Before Opening a Pull Request:** Run an interactive rebase
   (`git rebase -i`) to **squash** your draft commits into clean, logical
   blocks matching the format above.
3. **Amending:** Use `git commit --amend` for minor adjustments to your last
   commit instead of creating unnecessary new ones.

> **Agents working in this repo** follow additional restrictions (AGENTS.md,
> rule 10): they never amend, rebase, or force-push. Interactive rebase and
> squashing are human PR-preparation activities.
