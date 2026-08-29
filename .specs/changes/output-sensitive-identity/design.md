# Design: output-sensitive-identity

## Context

- Single-operator, internal-only deployment; the network is the authorization boundary.
  SQLite with Litestream replication; the single-writer assumption holds and every writer keeps transactions short.
- Version `0.1.3` at planning time; this change ships as `0.2.0`.
  SemVer 0.x permits the break in a MINOR bump; the pave runbook in this document is the migration plan's replacement.
- The 2026-08-27 code review (record in `._scratch/2026-08-28-code-review-findings.md`) produced the High/Medium findings this change remediates.
  The derivation-key redesign follows the analysis and plan in `._scratch/2026-08-28-l4-derivation-key-implementation-plan.md`, amended by the decisions below.
- The `keyterm-extraction-foundation` change is open in parallel and will need a rebase against this change's identity and key contracts.
- Raw inputs (KaraKeep export, source documents, blob artifacts) live outside the database and permit replay; the confirmation-gated corpus backfills (`enqueue_backfill`, `enqueue_extraction_backfill`) already exist and are the replay mechanism.

## Decisions

### Decision: Rebuild instead of in-place data migration

**Chosen:** Delete the database, re-ingest from the KaraKeep export, and replay all derived stages in DAG order through the existing confirmation-gated backfills.
One pave, executed as the final phase after every schema- and key-affecting change has landed.

**Rationale:** The in-place alternative required preflight classification of every stored run, in-place key recalculation, applicability backfill, a reversible downgrade, and a resolved deletion policy for unrecoverable records — the largest and riskiest part of the original plan.
The rebuild deletes all of it, answers the open deletion question ("delete all derived state, replay"), and exercises the replay path the project's principles promise.
Costs accepted by the owner: one full-corpus inference re-spend, loss of superseded history and prior event trails, and reminting of every internal identity.

**Alternatives considered:**

- In-place key recalculation (the original plan): preserves history; high machinery cost and an unresolved deletion boundary.
- Partial pave (keep conversion tier, wipe graph tier): cheaper, but the owner chose the full rebuild, which also collapses the Alembic history cleanly.

### Decision: One pave, last

**Chosen:** All phases land before the single pave; the pave is Phase 4's cutover.

**Rationale:** Every pave re-spends the corpus inference cost, so the pave count must be exactly one.
Sequencing it last also makes the pave the change's end-to-end verification on real data: fresh chunk minting, FTS writes, search joins, applicability recording, and exact console counts all run against a real corpus.

### Decision: Opaque keys through one shared helper

**Chosen:** Every shipped key constructor routes through `aizk.pipeline.identity.derivation_key`: SHA-256 over canonical JSON of `{"inputs": …, "upstream": []}`.
Graph stages pass no `upstream_keys`.
Run version stamps gain `derivation_key_format = "sha256-output-v1"`.

**Rationale:** One hash boundary prevents per-stage drift; an opaque digest makes the no-parsing prohibition physically enforceable (`rg 'json\.loads\([^\n]*derivation_key' src/aizk` must return nothing).
The algorithm is design-owned so the spec's portability contract survives a future algorithm change with a format-stamp bump.

**Alternatives considered:**

- Raw canonical JSON (status quo): transparent keys invited structural reads (`_recorded_upstream`), which this change removes; unbounded length.
- Removing the `upstream_keys` parameter: deferred — other consumers may legitimately consume an upstream key as semantic data; repository-wide inspection can retire it later.

### Decision: Split fingerprints — the key composes an input and a config fingerprint

**Chosen:** Each stage computes two fingerprints and composes the key from them:

```python
input_fingerprint = hash(exact_data_consumed)  # per-stage input lists below
config_fingerprint = hash(resolved_stage_config)  # resolved model config, prompts, versions, policies
derivation_key = hash({"input": input_fingerprint, "config": config_fingerprint})
```

The input side per stage, with its config counterpart:

- **chunking** — input: full markdown content hash; config: `splitter_version`.
- **summary** — input: full markdown content hash; config: resolved model config, summary prompt, `summary_version`.
- **contextualization run** — input: summary text hash, ordered chunk content keys; config: `splitter_version`, context prompt, `context_version`, `context_window_policy`, resolved model config.
- **contextualization row** — as the run, with the working chunk and its 2p/1n window in place of the full ordered list.
- **extraction** — input: ordered `{chunk_key, input_kind, input_text_hash}` items in document order; config: `extractor_version`, `materializer_version`, `input_policy`.

Fingerprint hygiene: every new durable fingerprint is a full-width SHA-256 — not the 64-bit `_stable_hash` truncation and not the stored `markdown_hash_xx64` column (stages hash the markdown they already load).
"Resolved model config" hashes the provider, model, and generation settings a profile resolves to, so a mutable alias such as `model_profile = "default"` cannot change configuration without changing the key.

Generation and persistence compute these from the same pure functions; persistence re-verifies the planned input set inside the write transaction before committing a run or an applicability record.

**Rationale:** The split makes the currentness tri-state computable from persisted columns: with one flat preimage, "payload equal but config changed" and "payload changed" are indistinguishable without re-deriving content.
It also makes reconciliation's comparison unambiguous: the candidate key is always computed from the current inputs **and the currently-resolved config** — never from the config stored on the run being evaluated, which would compare a run against itself on the config axis and reuse output across a prompt or model change.
`splitter_version` stays in the contextualization config because the contract declares it part of the input artifact's interpretation.
Excluded on purpose: `summary_derivation_key`, `chunking_derivation_key`, `contextualization_run_id`, `context_version` from extraction — producer history, not payload.

### Decision: Stage-owned provenance and applicability tables

**Chosen:** New rows, owned by their stages (names finalized at implementation):

- `ExtractionRunInput(run_id, upstream_run_id, input_policy, input_fingerprint)` — immutable original provenance, written with the run.
- `ContextualizationRunInput(run_id, summary_run_id, chunking_run_id, input_fingerprint)` — run-level original provenance, written with the run and authoritative for generation-level currentness; the per-variant `summary_run_id`/`chunking_run_id` stay for row-level tracing; exists even for a zero-chunk run, and every variant row's pair must agree with it.
- `ContextualizationRunApplicability(run_id, summary_run_id, chunking_run_id, input_fingerprint)` — one row binds the summary and chunking generations as a single input set.
- `ExtractionRunApplicability(run_id, upstream_run_id, input_policy, input_fingerprint)`.
- Chunking/summary applicability to a newer `conversion_output_id` whose content fingerprint matches the run's direct input.
- **Materialized output fingerprints**: each stage persists an observable fingerprint of the output downstream consumes, written at production time — the summary's text hash on the summary record, a chunk-set fingerprint on the chunking generation, a selected-text fingerprint for extraction inputs — so currentness classification is fingerprint equality over persisted columns and nothing re-loads or re-hashes content.

Two invariants enforced at these rows' writes:

- **Pairing**: an input set is valid only when its summary and chunking runs resolve to one conversion output — the same current output, or both applicable to the same output; source scope alone is insufficient.
- **Retention**: a run referenced by retained provenance or applicability is not compactable; compaction removes a downstream closure first; the baseline traceability contract already implies this, stated here so superseded-run compaction (which uses logical references, no FKs) cannot dangle.

**Rationale:** The run-level input record closes three gaps at once: a zero-chunk run previously had no provenance row anywhere, nothing enforced that all variants in a run name one input pair, and generation-level currentness had to scan variant rows.
Binding summary and chunking in one row (not two independent relations) is what makes the spec's "not split across input sets" rejection enforceable by construction.
Append-only rows with write-phase revalidation keep reuse decisions inside the existing short writer transaction; a mismatch raises a retryable stale-plan outcome and commits nothing.

**Alternatives considered:**

- Engine-owned generic edge table: violates the domain-core/engine split; stages own their input semantics.
- Timestamp-based "current" selection: rejected — currentness selects by matching input identifiers, never by recency.

### Decision: `scope_id` becomes `Uuid`

**Chosen:** Retype `pipeline_runs.scope_id`, the contextualization-output and FTS `scope_id` columns, and `graph_chunks.source_id` to `Uuid`, alongside the planned `chunk_id`/`mention_id` (`_lo`/`_hi`) retype.
The FTS `scope_id` insert guard (`_assert_scope_key_form`) follows the type change.

**Rationale:** Every real scope is a source UUID; a future non-source scope can mint a namespace UUID (`uuid5`), so the string flexibility bought nothing while its cost recurred: the un-joinable seam, the full-corpus Python anti-join (review finding H2), and boundary conversions.
With the seam gone, extraction's pending derivation becomes the same SQL anti-join contextualization already has, with `limit` pushed into the query and exact console counts.
SQLAlchemy's `Uuid` supplies the per-backend serialization (native on Postgres, CHAR on SQLite).

**Alternatives considered:**

- Keep `str` and page the Python anti-join: bounded but still scan-shaped, and it preserves a seam whose justification was speculative.
- SQL join on a serialized text form: backend-dependent UUID rendering; rejected before and stays rejected.

### Decision: Per-pass admission ceiling reuses `MAX_BULK_SELECTION`

**Chosen:** `run_admission_pass` caps each pass's admissions at `MAX_BULK_SELECTION` (100), independent of `queue_max_depth`.

**Rationale:** The review's M2: with `enabled=True, depth=0`, a pass enqueued the whole pending corpus inside one `begin_immediate`, contradicting the short-write contract.
The ceiling fixes the violation at its point of failure (transaction length) while keeping "unlimited, drain everything" a legal configuration — repeated passes drain the remainder.
Reusing the published bulk bound avoids a second magic number.

**Alternatives considered:**

- Config-validation error on `enabled` + `depth=0`: forbids the natural single-operator mode and forces an arbitrary depth choice.

### Decision: Creation events emitted inside the enqueue primitives

**Chosen:** `enqueue_document` / `enqueue_extraction` return `(job, created)`; on `created=True` they call `record_transition` in the same transaction, with an `origin` (`intake` | `admission` | `backfill`) and principal threaded from the caller. Intake routes drop their redundant pre-lookup and use `created` for 201-vs-200.

**Rationale:** Caller-side emission is the forgettable shape that produced the gap (review finding H4 — missed by eight reviewers).
Inside the primitive, a future caller cannot create spend-committing work without an event.
`(job, created)` serves both the event decision and the intake status code with one signature change.

### Decision: Operator transitions are capacity-exempt and reset the attempt counter

**Chosen:** `apply_retry` and `apply_extraction_readmission` stay outside `check_capacity` (now spec-documented), and both reset `job.attempts` to `0`.

**Rationale:** They create no unit; refusing them blocks remediation of work already admitted, and `MAX_BULK_SELECTION` bounds each action.
On attempts: an explicit operator requeue grants a fresh retry budget — readmission already did this; retry now matches, resolving the review-noted inconsistency in readmission's direction.

**Alternatives considered:**

- Headroom-truncating operator actions (consistency with backfill): backfill creates units, transitions don't; the consistency argument conflates the two.
- Retry keeping the prior count: made an operator retry of a max-attempts unit a no-op in effect.

### Decision: Extraction reconciliation rides the re-admission action

**Chosen:** The re-admission action's eligibility widens from "stale" to "not current" (stale or needs-reconciliation).
The requeued unit's worker computes the candidate key from current inputs and currently-resolved config: on a match with the active run it records the applicability relation and succeeds without invoking the extractor; on a mismatch it re-extracts and supersedes.
The monitor marks the two classes distinctly.

**Rationale:** Something must write the applicability row for a source whose upstream regenerated equivalently — extraction's work-unit is source-keyed and never re-pends, so without a trigger the needs-reconciliation state is unreachable and its relation is never recorded.
Contextualization needs no analogue: its pending derivation keys on `conversion_output_id`, so a regenerated output re-pends the source and the normal worker path compares before any per-chunk model call.

**Alternatives considered:**

- A separate operator "reconcile" action with a guaranteed no-spend profile: earns a second surface when the expensive path is expensive; extraction's producer is the pinned CPU NER model, so the worst case of one shared action is a cheap, correct re-extraction.
- Automatic reconciliation on classification: violates the contract that no classification admits work automatically.

### Decision: Alembic re-baselined at the pave

**Chosen:** Squash the migration history to one baseline revision matching the final ORM schema.
The runner refuses a database stamped with any pre-rebuild revision, with an error naming the rebuild path.

**Rationale:** The pave means no data crosses the boundary, so the historical chain (row repointing, backfills, conditional downgrades) is machinery for data that will never exist; keeping it is dead code that still must pass CI.
The legacy-refusal check replaces the old baseline-stamp procedure as the guard against a stale production snapshot meeting a new binary.

**Alternatives considered:**

- Keep the chain: preserves upgrade-path tests against old snapshots that the pave makes meaningless.

### Decision: Pave runbook

**Chosen:** The pave is a restart, not a migration event.
Executed once, when Phase 4 lands:

1. Land `0.2.0`: all phases green, full suite passing.
2. Stop services; archive the old database (file, WAL, config, binary version).
   The archive is the tested rollback path until the owner accepts the new corpus, a reference copy afterward.
3. Create the fresh database via the single baseline migration; start services at `0.2.0`.
4. Smoke-validate: ingest a few arbitrary inputs and confirm propagation on the console — conversion through extraction, creation events in the drill-down trail, exact counts, search hits.
   This is the change's acceptance, and it executes the key delta scenarios on real data (fresh chunk minting, FTS writes, applicability path).
5. Resume normal operation.
   Corpus repopulation is ordinary ingestion at the owner's pace; the console's pending/failed/stale surfaces are the standing answer to "what's processed," and the confirmation-gated backfills remain the moment spend is decided.

No old-database manifest or reconciliation gates the pave: the new corpus is defined by what the owner ingests, not by what the old database held.
There is no pave resume procedure because there is no pave state machine — pending work is derived from current state, enqueues are idempotent, and backfills are re-runnable, so an interrupted repopulation is simply a corpus with pending work.
The `0.2.0` binary is never run against the old database: deploying it and creating the fresh database are one act.
The pave never invokes the splitter, an LLM, or NER from a migration — all producer work runs through the normal work-unit path.

## Architecture

```text
conversion output ──▶ chunking run ──┐
        │                            ├─▶ contextualization run ──▶ extraction run
        └──────────▶ summary run ────┘         │    ▲                  │    ▲
                                               │    │                  │    │
   provenance (immutable, exact input) ────────┘    │                  │    │
   applicability (append-only, "also valid for") ───┘──────────────────┘────┘

reconcile(source):
  candidate = key(input_fingerprint(current inputs),
                  config_fingerprint(currently resolved config))   # never the run's stored config
  candidate == active_run.derivation_key and outputs complete
      → record applicability, reuse run, zero producer calls
  differs (data or config) or outputs incomplete
      → new generation (confirmation-gated where blast radius is large)
```

Work-unit creation (any path: intake / admission / backfill) → enqueue primitive → `INSERT` + creation event, one transaction.
Pending work = SQL anti-join over current state; stale work = anti-join over applicability; neither admits work automatically.

## Risks

- **Corpus inference re-spend**: repopulation re-runs summary and per-chunk calls over whatever the owner ingests; the confirmation-gated backfills are the decision point, and the changes-before-pave rule (converter, default model) keeps it to one full pass.
- **Non-deterministic regeneration**: post-pave contextualized text differs from pre-pave text.
  Nothing external pins chunk-level artifacts (owner-confirmed); FTS and mentions rebuild in DAG order.
- **Open `keyterm-extraction-foundation` change**: its artifacts predate these contracts; rebase it after this change's deltas land, before its implementation resumes.
- **Squash drops upgrade-path coverage**: accepted; the legacy-refusal error is the guard, and it is itself tested.
- **Reconciliation race**: the inputs changing — or the active run being superseded — between read and write produces the retryable stale-plan outcome; the write-phase revalidation is the enforcement point and carries its own test.
- **Retype touches the just-landed FTS guard**: `_assert_scope_key_form` and its 10 parametrized tests are rewritten with the `Uuid` storage form in the retype phase; the raw FTS SQL binds values through the ORM `Uuid` type's bind representation, never hand-formatted strings — expected churn, not regression.
