# Software Engineer Assessment — Document Translator

**Duration:** ~3 days

---

## The Challenge

Build a document translation service your team would be willing to operate.

**Input:** A file + a target language
**Output:** The same file, translated

We are not hiring you to write prompts. We are hiring you to build a system that
happens to have an LLM inside it. Treat the model as one dependency among several —
a slow, expensive, occasionally wrong one.

---

## Minimum Viable Product

A web interface that:
1. Accepts PDF uploads
2. Translates the content using an LLM
3. Outputs a translated PDF

A real document takes minutes, not seconds. How you handle that is your call — we will
not tell you the shape of the answer, but we will test it: **during review we kill and
restart your containers mid-translation.** Nothing should hang forever, and nothing
should be silently lost or corrupted.

Everything beyond this is up to you.

---

## Hard Requirements

| Requirement | Details |
| --- | --- |
| **Backend** | Your choice of language and framework |
| **AI provider** | Real OpenAI API (key provided). Keep it behind an interface — your tests must not call it. |
| **Agents** | At least one stage of the pipeline uses the OpenAI Agents SDK (`openai-agents`) with tool calling, somewhere an agent genuinely earns its place over a plain completion. Explain in the README why *there* and not elsewhere. |
| **MCP** | Ship an MCP server exposing the translator, so a document can be translated from Claude Code / Cursor. README gives the exact config and a three-step verification recipe. |
| **Formats** | At least two input formats. The second one exists to prove the first one's design was extensible. |
| **Frontend** | Your choice |
| **Delivery** | Git repo with `docker compose up --build` that works anywhere |
| **README.md** | Architecture, the decisions behind it, and a testing guide |
| **PROMPTS.md** | How you used AI **to build this** — including what you rejected |
| **DECISIONS.md** | What you cut and why, trade-offs taken knowingly, your measured cost per document and p95 latency, and what you'd build with three more weeks |

---

## API Key

(See the email for the API key)

Budget is generous, but manage development costs. Keep costs low while building; spend on
better models where quality actually demands it. We expect you to know what your service
costs per document, because we will ask.

---

## What We're Evaluating

### 1. Agency
Your capacity to make decisions and push the project as far as it deserves to go. There is
no checklist. Show us what you think a great document translator looks like, and be able to
defend where you stopped.

### 2. Engineering Fundamentals
This is the heaviest weight in the review.

- Data model and API design — what the core objects are, and what a client can do with them
- Layering: the web app and the MCP server are two front doors onto one core
- Concurrency: two users and one 200-page document arriving at the same time
- Idempotency and retries — a retried unit of work must not double-bill or corrupt output
- Failure handling: corrupt PDF, scanned PDF, 400-page PDF, provider 500, provider timeout,
  a language pair the model handles badly
- Tests that would catch a regression someone else introduces next month
- Observability: your service breaks in production at 3am and you are on call. What do you
  look at, and is it in the repo?

### 3. Product Judgment
Decide who this is for and say so in the README. Write your own three acceptance criteria
and meet them. Pick one measure of translation quality, measure it, and report the number —
a crude honest metric beats a sophisticated unmeasured one. We care as much about what you
chose **not** to build, and whether you can say why.

### 4. AI Leverage
PROMPTS.md documents how you used AI to build this: what you delegated, what you wrote
yourself, and at least three cases where you rejected or corrected what the AI produced.
We are reading for judgment, not prompt volume. An honest log of a failed approach is worth
more than a tidy one.

### 5. User Experience
- Branded for **Stark** (find the branding online)
- Intuitive, with clear feedback while a translation is in flight
- Graceful, specific error handling — "Something went wrong" is not error handling

---

## Ideas to Explore

Not a checklist. Pick what makes sense, ignore what doesn't, add your own.

- More formats: Word, Excel, PowerPoint, HTML, Markdown
- Preserve formatting, styles, tables, images
- Translation memory or caching — the same paragraph twice should not cost twice
- Glossary / terminology enforcement (brand names, model names, units)
- Multiple target languages at once
- Side-by-side preview, or editing a translation before download
- Batch processing, translation history
- MCP tools beyond a single `translate_file`
- Auto-detect source language
- Scanned PDFs, very large files, complex layouts
- A cost cap per document
- Tracing and structured logging
- Quality validation pass

---

## Questions to Consider

These will shape your architecture. Think about them early — we will ask about them.

- How do you translate a document that doesn't fit in one context window, and keep it
  coherent across the seams?
- How do you make it fast? What did you measure before and after?
- Where does an agent earn its keep, and where is it just an expensive `if`?
- What belongs in your MCP server's tool surface, and what doesn't? A tool per REST
  endpoint is usually the wrong answer.
- How would you add a new file format in 10 minutes? Your second format is the evidence.
- What happens when a user uploads an unsupported, corrupted, or enormous file?
- What does one translated document cost you, and what dominates that cost?

---

## How We Test

1. Clone the repo fresh
2. `docker compose up --build`
3. Upload a PDF, get a translated PDF back
4. Configure your MCP server from your README alone and translate a document without
   touching the web UI
5. Restart the stack mid-translation and watch what happens
6. Feed it the edge cases: corrupt file, huge file, unsupported type
7. Go through the features you describe in the README
8. Read the code, then talk it through with you

---

## Before Submitting

- [ ] Fresh `git clone` works
- [ ] `docker compose up --build` runs without errors
- [ ] Upload a PDF → get a translated PDF back
- [ ] Second format works
- [ ] MCP server connects from a clean Claude Code / Cursor install using only the README
- [ ] A document can be translated end to end through MCP alone
- [ ] Test suite passes
- [ ] README includes a testing guide
- [ ] PROMPTS.md and DECISIONS.md are complete and honest
- [ ] No secrets committed
- [ ] `.env.example` provided

---

## What Impresses Us

- A core that both front doors sit on without duplication
- Thoughtful failure handling that survives us trying to break it
- MCP tools designed for how someone would actually use them from an editor
- Performance thinking backed by numbers you measured
- Security: file validation, input sanitization, resource limits
- Tests that pin down behaviour worth protecting
- Knowing your cost per document

---

## Tips

- Document your decisions honestly. Abandoned approaches are interesting.
- Polish matters, but not more than the thing working when we try to break it.
- If you run out of time, cut scope deliberately and say so in DECISIONS.md. Silent gaps
  read as oversights; stated ones read as judgment.

---

**Show us what you can do.**