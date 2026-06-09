# Design: Contextualization Checkpoint Resume

## Context

Contextualization runs as a single per-document work-unit (`graph/workunit.py:process_document`).
Its model work happens in three steps with one write transaction at the end:

```text
read_session (read-only):   summary_text = resolve_summary_text(...)   # reuse active OR fresh LLM, NOT persisted
(no transaction):           revisions     = generate_revisions(...)     # N LLM calls, NOT persisted
_begin_immediate (write):   summarize_document(...) + contextualize_chunks(...)   # persist summary + variants, commit
```

The generation steps run outside any write transaction so the single-writer SQLite lock (`BEGIN IMMEDIATE`) is not held across slow model calls.
Persistence is atomic: `record_run` (`pipeline/run.py`) creates each run `ACTIVE` and supersedes the prior active run under a partial unique index (one active run per `(stage, scope_key)`); `contextualize_chunks` treats a short variant set as torn and regenerates.
A failure inside `generate_revisions` propagates before the write transaction, so **nothing** is persisted — and on a document's first contextualization there is no active summary run for `resolve_summary_text` to reuse, so each retry mints a fresh, non-deterministic summary.

Reuse identities already exist and are input-deterministic: `_summary_derivation_key` (markdown hash + `summary_version` + summary prompt hash + model profile) and `_variant_row_derivation_key` (summary identity + working chunk + 2p/1n neighbors + `splitter_version` + window policy + context prompt hash + model profile + `context_version`).
The summary key does **not** embed the source; the variant key does, transitively, because `chunk_id = derive_chunk_id(doc_id, …)` and `doc_id = str(aizk_uuid)`.

This change must add durable per-output checkpointing without weakening the lock discipline, the atomic-on-success supersession, or the shared run lifecycle.

## Decisions

### Decision: An LLM-output memo beneath the run model, not a resumable run state

**Chosen:** A dedicated memo table caches validated model outputs (summary and per-chunk revisions) keyed by their input-deterministic derivation keys, written incrementally during the generation phase.
Run recording, activation, and supersession at the final transaction are unchanged; the memo is read during generation to skip model calls and is never read as product state.

**Rationale:** The memo isolates the only thing worth checkpointing — expensive, non-deterministic model output — from run membership and activation.
Correctness still flows entirely through runs and derivation keys; the memo is a pure cost cache.
This keeps the blast radius inside `chunk-contextualization` and leaves the shared `PipelineRun` lifecycle untouched for every other stage.

**Alternatives considered:**

- **Resumable run state (a "building"/non-active status):** record the variant run up front and fill it in.
  Rejected — it changes `RunStatus` and `record_run` semantics in `pipeline-stage-runtime`, a mechanism shared by every stage, to solve a problem local to one stage.
- **Commit the summary run early (own transaction before `generate_revisions`):** would let active-summary-reuse stabilize the summary across retries.
  Rejected — it splits the single write transaction and activates a summary run before its generation is proven complete, so a permanently-failed unit leaves an active summary run with no variants, having superseded the prior one.
  The memo keeps supersession atomic-on-success.

### Decision: Memo the summary output, not only revisions

**Chosen:** Both the document summary and each per-chunk revision are memoized.

**Rationale:** A revision's derivation key includes the summary identity, which includes the summary text hash.
If only revisions were memoized, a retry that re-generates a fresh (different) summary would shift every revision key and miss the entire revision memo.
Memoizing the summary — keyed by its source-independent derivation key — re-stabilizes the summary text across retries, which re-stabilizes every downstream revision key.
The summary key is input-deterministic (no model output in it), so it reliably re-hits.

### Decision: Memo-aware `resolve_*` wrappers; pure `generate_*` functions preserved

**Chosen:** The pure model-I/O functions stay pure: `generate_summary_text` and `generate_revisions` keep no DB access.
The memo/reuse logic lives in `resolve_*` counterparts — `resolve_summary_text` (upgraded from read-only reuse to memo-aware, now taking `engine` and committing autonomously, with its docstring contract updated) and a new `resolve_revisions`.
A pure per-chunk generation helper is extracted so `resolve_revisions` can generate a single chunk on a memo miss while `generate_revisions` retains its bulk pure form.
The summary identity needed during generation (before any `DocumentSummary` row exists) comes from a text-based `_summary_identity_from_text(summary_text, summary_version, summary_derivation_key)`, which the existing `_summary_identity(summary, …)` delegates to.

**Rationale:** This follows the codebase's existing `generate_*`/`resolve_*` split, keeps the pure functions usable by direct callers and tests, and confines the contract change (read-only → memo-writing) to one explicitly-documented function.
Factoring the summary identity off the text rather than the persisted row is what lets the generation phase build variant derivation keys before persistence.

### Decision: Source-scoped memo identity — `(kind, scope_key, derivation_key)`

**Chosen:** Memo uniqueness is `(kind, scope_key, derivation_key)` where `kind ∈ {summary, revision}`, `scope_key = str(aizk_uuid)`, and `derivation_key` is the respective JSON derivation key.
A single table with a `kind` discriminator.

**Rationale:** `scope_key` is load-bearing only for the summary kind, whose derivation key omits the source — without it, two distinct sources with byte-identical Markdown share a summary entry.
The revision key is already source-distinct via `chunk_id`, so `scope_key` is redundant-but-harmless there and keeps the schema uniform.
Source scoping also makes the prune key set well-defined per source and matches the capability's universal `aizk_uuid` scoping.
A single discriminated table avoids two near-identical tables and two migrations; the keys never collide across kinds because their JSON shapes differ and `kind` partitions them regardless.
The `kind` domain is enforced at the persistence boundary — a `CHECK (kind IN ('summary', 'revision'))` constraint plus a `Literal`-typed Python boundary with single-source-of-truth constants — so a typo cannot create a durable but unreachable entry (it fails closed loudly rather than silently wasting model calls and orphaning rows).

**Alternatives considered:**

- **`(kind, derivation_key)` global keys:** rejected — allows cross-source summary reuse (an anomaly against the capability's source scoping) and makes "exclusive claim prevents contention" false, since two same-source units sharing a summary key could race.
- **Separate `summary_memo` / `revision_memo` tables:** rejected — no benefit over a discriminated single table; two migrations and two query paths for one concept.

### Decision: Present-empty is a hit, distinct from absent

**Chosen:** A memo row stores `output_text` as TEXT, with `''` a legal value meaning "the model judged this chunk already self-contained."
A lookup returns the row or `None`; `None` is a miss (generate), `''` is a self-contained hit, non-empty is a revision hit.

**Rationale:** An empty revision is a valid, validated output.
If absence and empty were conflated, every already-self-contained chunk would look like a miss and be re-invoked on every retry — re-introducing the cost cliff for exactly the cheapest-to-reuse chunks.

### Decision: Validate at the memo-write boundary; only validated output is retained

**Chosen:** The existing validations (`_validate_summary_text`, `_validate_contextualized_text`) run in the generation phase, immediately before a memo upsert.
An output that fails validation is not written to the memo; the attempt fails and a retry re-invokes the model.
The final persist consumes already-validated memo outputs.

**Rationale:** If raw output were memoized, a transient overlong generation would become a durable result that fails validation on every retry — a permanent failure the model might otherwise recover from on a fresh call.
Validating before retention makes invalid output non-durable.
Validation is pure and cheap, so the final persist may re-validate defensively without cost concerns.

### Decision: No write transaction spans a model call

**Chosen:** Each validated output is committed to the memo in its own brief transaction, interleaved with the lock-free model calls: model call (no lock) → brief write transaction → idempotent upsert-and-read of one row → commit → next call.
The write is `INSERT … ON CONFLICT(kind, scope_key, derivation_key) DO NOTHING` followed by a read of the stored row; the helper returns the **authoritative stored value**, and the caller uses that returned value — never its own just-generated value — for all downstream derivation and persistence.

**Rationale:** The generation phase was deliberately lock-free; adding memo writes must not regress that.
Holding `BEGIN IMMEDIATE` across N model calls would serialize every other writer for the whole job.
Short autonomous commits hold the lock only for a single-row insert.
The idempotent write makes a retry re-deriving an existing key a no-op.
Returning the authoritative stored value is what makes the benign same-source contention case (a stale-vs-current re-enqueue with identical Markdown) actually benign: model output is non-deterministic, so a loser's valid output may differ from the winner's; adopting the stored (winner's) value means the summary text used to derive revision keys always equals the retained summary text.
Using its own losing value instead would let a unit persist a generation the memo no longer matches — orphaning the winner's summary row past the key-exact prune and forcing re-generation on the loser's next retry.

### Decision: Active-run reuse is rechecked in the generation phase, before the memo

**Chosen:** Before generating per-chunk revisions, the generation phase checks for a complete active variant run whose run-level derivation key matches (mirroring the active-summary-run check `resolve_summary_text` already performs).
On a match, it skips revision generation entirely; the persist phase reuses that active run.
Only on no match does it fall through to the memo-or-generate path per chunk.

**Rationale:** The success-path prune deletes the memo entries a completed generation consumed, so after a successful run the memo no longer holds those revisions.
Without this precheck, a re-execution of an already-completed document would miss the (pruned) memo and invoke the model for every chunk — violating the "zero invocations on completed re-execution" contract.
The precheck restores zero-invocation re-execution and supersedes the current code's wasteful behavior of generating all revisions and then discarding them at the persist-phase reuse check.
The summary side already has this precheck; this extends the same discipline to revisions.

**Plan-vs-apply guard (defense-in-depth):** The lock-free generation phase plans against the active runs, then the persist transaction applies.
Under the freshness gate, single-writer, and idempotency-dedupe invariants, a run planned for reuse cannot be superseded by a still-valid writer between plan and apply — any superseder is a newer conversion output (skipped by the in-transaction freshness gate before persist), the same output (deduped to one unit), or an older one (skipped by its own gate).
So a divergence is not expected to be reachable.
To keep that invariant from failing silently if it is ever weakened, the apply step revalidates the plan and fails **retryably** (a `StalePlanError`, mapped to a retryable outcome — distinct from the permanent `ValueError`) rather than corrupting provenance or permanently failing: `summarize_document` rejects reusing an active summary whose text differs from the revisions' planned summary, and `contextualize_chunks` (under a `reuse_only` flag set when the generation phase signalled active-run reuse) rejects applying when the planned complete active variant run is no longer present.
A retry then re-resolves and regenerates the missing work outside the write lock.

### Decision: Key-exact success-path prune; defer permanent-failure and TTL sweeps

**Chosen:** When the final transaction persists a generation, it also deletes exactly the memo entries that generation consumed — its summary key and its per-chunk revision keys (all under this `scope_key`).
Pruning of memo rows for a permanently-failed unit, and any TTL sweep, are a named follow-up, not in this change.

**Rationale:** Once `DocumentSummary` / `ContextualizedChunk` hold the outputs, the consumed memo rows are redundant; a later re-execution of the completed document needs no memo because the active-run reuse precheck (above) gives it zero model calls.
Key-exact deletion (not a source-wide wipe) protects a concurrent same-source attempt working under _different_ keys (e.g., a re-contextualization under changed inputs).
Permanent-failure pruning would require a hook in the runtime/handler terminal-status path — outside this capability — so it is deferred; the residue is bounded (a permanently-failed unit does not retry, so its rows are bounded per document) and the memo carries `scope_key` and `created_at` so a later sweep is actionable.

## Architecture

```text
process_document(engine, client, …)
│
├─ generation phase  ── lock-free model calls, short autonomous memo commits ──
│   resolve_summary_text:
│     k = (summary, aizk_uuid, summary_derivation_key)
│     active-summary-run reuse?  ── yes ─→ summary_text          (no model call)
│     memo.get(k)               ── hit ─→ summary_text          (no model call)
│     else: t = client.generate(...);  validate(t);  summary_text = memo.upsert_and_read(k, t)   (authoritative value)
│   resolve_revisions (summary_identity from _summary_identity_from_text):
│     complete active variant run matches run-derivation-key?  ── yes ─→ skip generation  (no model calls; persist reuses it)
│     else per chunk c:
│       k = (revision, aizk_uuid, variant_row_derivation_key(c, summary_identity, …))
│       memo.get(k)  ── hit (incl. '') ─→ revision              (no model call)
│       else: r = client.generate(...);  validate(c, r);  revision = memo.upsert_and_read(k, r)
│          each memo.upsert_and_read = its own BEGIN IMMEDIATE … COMMIT (one row), returns the stored (authoritative) value
│
└─ persist phase  ── single _begin_immediate transaction (unchanged shape) ──
    summarize_document(...)         # record summary run (ACTIVE), write DocumentSummary
    contextualize_chunks(...)       # record variant run (ACTIVE), write ContextualizedChunk rows
    memo.delete_keys(scope_key, [summary_key, *revision_keys])   # key-exact prune
    commit  ←  supersession is atomic here; nothing above is readable as a run/summary/variant
```

Storage: one table `graph_contextualization_output_memo(id, kind, scope_key, derivation_key, output_text, created_at)`, unique `(kind, scope_key, derivation_key)`, following the `graph_*` naming of the existing graph tables, added by an Alembic migration in the graph tables tree (`conversion/migrations/versions/`).
Lookups index on the unique key.

## Risks

- **Lock contention from per-output commits**: each memo write takes the single writer briefly.
  Mitigation: one-row autonomous commits held only for the insert, never across a model call; this is strictly shorter than the existing per-document persist transaction and runs at chunk order-of-magnitude, well below the mention order-of-magnitude the next change introduces.
- **Memo growth on permanent failure**: deferred permanent-failure pruning leaves residue.
  Mitigation: residue is bounded per document and does not retry; `scope_key` + `created_at` make a later sweep actionable; flagged as a named follow-up so the gap is explicit, not silent.
- **Validation drift between generation and persist**: moving validation earlier risks two code paths diverging.
  Mitigation: reuse the same `_validate_*` functions at both boundaries; the persist-side check becomes a cheap idempotent re-validation rather than a second implementation.
- **Treating the memo as product state**: a future consumer (the explorer UI this change unblocks) could mistakenly read the memo.
  Mitigation: the memo is documented and tested as internal scratch; the spec contract (requirement 3) forbids any run/summary/variant becoming readable from retained work, and operator/explorer surfaces read the `DocumentSummary` / `ContextualizedChunk` projections only.
