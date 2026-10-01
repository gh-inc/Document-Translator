# PROMPTS.md

How AI was used to build this project — what was delegated, what was decided
by the human, and where AI output was rejected or corrected. Written for
judgment, not prompt volume, per the assessment brief.

**Working method:** the architecture was developed in dialogue with an LLM
coding assistant, then put under explicit adversarial review *before any code
was written*. The human's role in this phase: scope decisions, stack
confirmation, acceptance-criteria review, and the final call on every
contested point below. Implementation entries are appended as work proceeds.

---

## Log

### 2026-09-30 — Architecture brainstorming (design v1 → v3)

**Delegated to AI:** first-pass architecture for the whole service — queue
design, data model, pipeline, agent placement, MCP surface — proposed as
options with trade-offs, then redrafted twice under review.

**Rejected / corrected AI output:**

1. **The "zero duplicate billing" guarantee.** The initial design promised
   that a killed-and-resumed job's cost would equal the sum of unique chunk
   translations. Rejected as physically impossible over an external LLM API:
   an ambiguous timeout (provider processed the request, client never
   received the response) forces at-least-once provider invocation, and the
   Chat Completions API has no client idempotency keys. Replaced with an
   explicit guarantee boundary — exactly-once for *committed* results,
   at-least-once for *provider calls* — with duplicate spend measured in
   `chunk_attempts` and reported rather than hidden.

2. **Translated-context threading.** The initial design fed the previous
   chunk's *translation* into the next chunk's prompt for coherence — while
   simultaneously claiming 8-way chunk parallelism. Corrected: that is a
   serial dependency chain wearing a parallel costume. Coherence now comes
   from the persisted TranslationPlan + glossary + neighboring *source*
   blocks only; a translated-context polish pass is documented as a cut.

3. **Universal Document IR.** The initial format strategy normalized PDF and
   DOCX into one rich layout IR (typed blocks, style/geometry semantics).
   Rejected after a risk pass: over-engineering for a 3-day MVP, and style
   mapping degrades exactly where translated text changes length. The
   Markdown-bridge alternative was rejected alongside it (fatal layout loss).
   **Revised to the Opaque Metadata pattern:** the core handles only `seq` +
   `source_text`; format specifics travel as opaque JSON on the Block;
   renderers re-open the original file as the canvas. Full rationale in
   DECISIONS.md §2.

**Assessment of the collaboration pattern so far:** the AI was most useful
as a fast generator of complete, internally consistent first drafts — and
most dangerous exactly there: fluent, plausible guarantees that do not
survive contact with distributed-systems reality. Every invariant in the
final design either came from the human reviewer or survived an explicit
"what can this system actually promise?" cross-examination.

---

_Implementation entries to follow._
