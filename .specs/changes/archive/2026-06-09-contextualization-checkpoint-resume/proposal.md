# Proposal: Contextualization Checkpoint Resume

## Intent

Contextualization is the LLM-cost driver of the graph stage: one summary pass per document plus one revision pass per chunk.
Today that cost is all-or-nothing per document.
The summary and every per-chunk revision are generated in a lock-free phase and persisted in a single write transaction only after all of them succeed (`workunit.py:279-290`); a failure partway through `generate_revisions` propagates before that transaction, so nothing is persisted.
A retry then re-pays for every chunk — a document that fails on chunk 499 of 500 discards 499 good model calls and recomputes all 500.

This change makes the model work of a partially-completed attempt durable, so a retry recomputes only the chunks it has not already produced.
It de-risks the cost cliff before it bites at scale (large documents, a flaky model endpoint) and before any UI is built on top of the stage.

## Scope

Single capability: `chunk-contextualization`.

**In scope:**

- A **source-scoped contextualization LLM-output memo**: a durable cache of model outputs whose uniqueness is `(kind, scope_key, derivation_key)`, where `scope_key` is the durable source identity (`str(aizk_uuid)`) and `derivation_key` is the input-deterministic identity that already governs reuse — the summary output keyed by the summary derivation key (markdown hash + `summary_version` + summary prompt identity + model profile), and each per-chunk revision keyed by its per-row variant derivation key (summary identity + working chunk + 2p/1n neighbors + `splitter_version` + window policy + context prompt identity + model profile + `context_version`). `scope_key` is load-bearing for the summary kind, whose derivation key does **not** otherwise embed the source (the revision key already does, via `chunk_id`); without it, two distinct sources with byte-identical Markdown would share a summary entry.
- **Validated, normalized output only**: an output is validated (the existing summary and chunk-relative revision-length checks) at the memo-write boundary, and only a passing output is memoized — an output that fails validation is never written, so a retry re-invokes the model rather than replaying durable junk.
  An empty revision (the model judged the chunk already self-contained) is a **valid** output stored as a present-empty entry, distinct from an absent entry, so a self-contained chunk is a hit rather than a perpetual miss.
- **Autonomous-commit writes** into the memo during the generation phase, under the invariant that **no write transaction spans a model call**: each validated output is committed in its own brief transaction (model call holds no lock → brief write transaction → upsert one row → commit → next call), so the single-writer lock is never held across a model call.
- **Resume on retry**: the generation phase resolves the summary and each revision from the memo before invoking the model, so a retry of a partially-completed unit invokes the model only for the outputs still missing.
- **Success-path prune**: when a generation is durably persisted, the final write transaction also deletes exactly the memo entries that generation consumed — its summary key and its per-chunk revision keys (now redundant, since the outputs are present in `DocumentSummary` / `ContextualizedChunk`).
  The prune is key-exact, not source-wide, so it cannot erase a concurrent same-source attempt's checkpoint rows under different keys.

**Out of scope:**

- The view UI for chunking/contextualization jobs and the source→markdown→chunk→contextualized-chunk explorer (the next change; this one is its prerequisite).
- Any change to the run lifecycle (`pipeline-stage-runtime`): runs are still recorded `ACTIVE` and supersede atomically at the existing single write transaction; the memo does not introduce a "building"/partial run state.
- **Memo as an operator/product surface**: the memo is internal scratch state, never a queryable projection; operator and explorer surfaces read the project-owned `DocumentSummary` / `ContextualizedChunk` projections, not the memo.
- **Permanent-failure and TTL memo sweeps** (a named follow-up): pruning memo rows for a unit that reaches a permanent terminal status would require a hook in the runtime/handler terminal path, outside this capability.
  Deferring is safe short-term because a permanently-failed unit's residual rows are bounded per-document and do not retry; this change records no extra signal for the sweep beyond `scope_key` and a creation timestamp.
- Per-document **summary**-level checkpointing beyond memoizing its single output (there is one summary pass; memoizing it is sufficient).
- Changing what records a completed run produces or their provenance/derivation linkage — contextualization output, idempotency, and traceability contracts are unchanged.
- Cross-document or cross-source reuse of memo entries — `scope_key` in the uniqueness confines every entry to its source.

## Approach

> Mechanism sandbox; contracts live in the delta spec, chosen mechanisms formalize in `design.md`.

The memo is a pure cost cache for non-deterministic model output, layered **beneath** the existing run/activation model, not a replacement for it.
Correctness still flows through runs, derivation keys, and supersession exactly as today; the memo only prevents re-paying for a model call whose deterministic input identity has already been computed in a prior attempt of the same unit.

- **Why the memo must cover the summary, not only revisions.**
  A per-row revision key includes the summary identity, which includes the summary text hash.
  On a document's first contextualization (or after its summary run was superseded), there is no active summary run for `resolve_summary_text` to reuse, so each retry mints a fresh, non-deterministic summary — a different text hash, hence different revision keys, hence a 100% miss against any revision-only memo.
  Memoizing the summary output (keyed by its input-deterministic derivation key) re-stabilizes the summary text across retries, which in turn re-stabilizes every downstream revision key.
- **Why not commit the summary run early instead.**
  Committing the summary run before `generate_revisions` would let active-summary-reuse stabilize the summary, but it splits the single write transaction and activates a summary run before its generation is proven complete — a permanently-failed unit would leave an active summary run with no variants, having superseded the prior one.
  The memo keeps supersession atomic-on-success and the final commit single.
- **The load-bearing lock invariant: no write transaction spans a model call.**
  The generation phase performs DB writes (the memo upserts) but must never hold a write transaction open across a slow model call, or the single-writer SQLite lock would serialize every other writer for the whole job.
  Each validated output is therefore committed in its own brief transaction — model call (no lock) → brief write transaction → upsert one row → commit → next call.
  Insert-or-ignore on `(kind, scope_key, derivation_key)` makes a retry re-deriving an existing key idempotent.
  `scope_key` confines every entry to a single source, so no entry is shared across sources — but two work-units for the _same_ source can still overlap (e.g. a stale-vs-current conversion re-enqueue whose byte-identical Markdown yields the same summary key), so contention on a shared key is possible.
  It is benign rather than impossible: a concurrent write is an insert-or-ignore of an identical validated value and resolves to the same row without corruption.
  Safety comes from the idempotent upsert, not from exclusivity.
- **Output is validated before it is memoized.**
  Validation (the existing summary-length and chunk-relative revision-length checks) runs at the memo-write boundary, not only at final persist; a failing output is not written, so a transient overlong generation does not become durable and force the unit to fail every retry.
  An empty revision is a valid memoized output recorded as present-empty, kept distinct from an absent entry.
- **Consumption is unchanged.**
  The existing atomic persist (`workunit.py:290`) still records the summary run and variant run and writes `DocumentSummary` / `ContextualizedChunk` from the resolved outputs; it simply reads memo-backed outputs, and in the same transaction deletes exactly the memo entries this generation consumed (key-exact, not source-wide).
