# Tasks: output-sensitive-identity

Phases land in order; each phase ends with the full suite and all hooks green.
The parallel track may run alongside Phases 1–3 but MUST complete before Phase 4.
Production never runs the new binary against the old database; the pave is part of deploying `0.2.0`.

## Phase 1 — Admission & observability (no schema)

### Capacity posture

- [ ] Correct `src/aizk/graph/README.md`: capacity is a creation gate over the actionable backlog; the network is the authorization boundary; remove the bounded-exposure claim.
- [ ] Cap `run_admission_pass` admissions at `MAX_BULK_SELECTION` per pass, independent of `queue_max_depth`.
- [ ] Test: `enabled` with `queue_max_depth=0` and pending work beyond the ceiling admits exactly the ceiling in one short transaction, remainder stays pending (`tests/graph/test_admission.py`).
- [ ] Test: repeated passes drain the pending set to empty with no capacity limit declared.
- [ ] Test: with headroom above the ceiling, a pass admits at most the ceiling; with headroom below it, at most the headroom.
- [ ] Test: cost of a bounded pending evaluation does not grow with the admitted corpus (statement/row-count assertion on two corpora differing only in admitted sources).
- [ ] Reset `job.attempts` to `0` in `apply_retry` (matching `apply_extraction_readmission`).
- [ ] Test: `apply_retry` and `apply_extraction_readmission` succeed while the stage is at capacity — both actions, both stages (`tests/graph/test_job_actions.py`).
- [ ] Test: both operator actions reset the attempt counter.

### Creation events and intake

- [ ] Change enqueue primitives (`enqueue_document`, `enqueue_extraction`, `enqueue_output`) to return `(job, created)`; update all production and test call sites.
- [ ] Thread `origin` (`intake` | `admission` | `backfill`) and principal into the primitives; emit the creation event via `record_transition` inside the creating transaction when `created` is true.
- [ ] Test per creation write-site: intake, admission pass, and backfill each produce exactly one creation event carrying the correct origin and principal — both stages.
- [ ] Test: a request resolving to an existing unit (dedupe branch) writes no creation event.
- [ ] Test: a rolled-back creation leaves neither the work-unit nor the event (co-commit).
- [ ] Intake routes: drop the redundant pre-lookup; decide 201-created vs 200-reused from `created`.
- [ ] Contract test: `POST /v1/contextualizations` and `POST /v1/extractions` return 201 for a new unit and 200 for a resubmission.
- [ ] Test: intake accepts a submission while automatic admission is disabled for the target stage.

### Console and small fixes

- [ ] `StageDescriptor`: enforce `pending_count`/`pending_list` both-or-neither in `__post_init__`; test rejects a half-declared capability.
- [ ] `enqueue_backfill`: pass `already_enqueued` as a set.
- [ ] Resolve the unconsumed `admission_adapter_for` surface: route the admission loop's adapter resolution through it; test the feature-detection scenario against the registry.

## Phase 2 — Retype (schema only; the pave carries the data)

- [ ] Retype to `Uuid` in the ORM: `pipeline_runs.scope_id`, contextualization output/memo `scope_id`, `graph_content_fts.scope_id` handling, `graph_chunks.source_id`, `chunk_id` (identity and every FK/reference column), `mention_id` and `mention_id_lo`/`mention_id_hi` — per the retype plan's boundary inventory.
- [ ] Add the Alembic revision; update schema-equivalence fixtures; round-trip test passes.
- [ ] Convert `UUID(scope_id)` / `str(source_id)` call sites; sweep the whole tree (`rg` including notebooks, scripts, docs) for every reference form.
- [ ] Rewrite the FTS insert guard and raw-SQL writes to bind through the ORM `Uuid` type's representation; rewrite the guard's parametrized tests for the new storage form.
- [ ] Extend the identity-naming conventions test to pin the new typing rule (no string-typed identity columns; enumerated from `SQLModel.metadata`).
- [ ] Rewrite `pending_extraction_sources` as a SQL anti-join with `limit` pushed into the query; parity test against the prior semantics on a seeded corpus.
- [ ] Wire limits through the console pending closures; test exact pending counts and a bounded listing for both graph stages.

## Phase 3 — Output-sensitive identity

### Fingerprints and keys

- [ ] Implement per-stage pure functions: `input_fingerprint`, `config_fingerprint` (full-width SHA-256), composed through `aizk.pipeline.identity.derivation_key`; unit tests assert every shipped key constructor returns the shared helper's digest for the same semantic inputs.
- [ ] Portability test: identical semantic inputs across two databases with different local ids yield equal keys.
- [ ] Resolved-model-config fingerprint: hash provider, model, and generation settings; test that a profile alias change with an unchanged resolution keeps the key, and a changed resolution under the same alias changes it.
- [ ] Stamp `derivation_key_format = "sha256-output-v1"` in new runs' version stamps.
- [ ] Remove `_recorded_upstream` and every structural read of a stored key; add a guard test asserting `rg 'json\.loads\([^\n]*derivation_key'` matches nothing under `src/aizk`.

### Provenance, applicability, output fingerprints (schema)

- [ ] ORM + Alembic revision: `ContextualizationRunInput`, `ExtractionRunInput`, `ContextualizationRunApplicability`, `ExtractionRunApplicability`, chunking/summary conversion-output applicability, and the materialized output-fingerprint columns (summary text hash, chunk-set fingerprint, extraction input fingerprint); equivalence fixtures updated; round-trip test.
- [ ] Persist output fingerprints at production time in each producer; test that downstream classification reads persisted columns only (statement assertions — no chunk/variant content loads).
- [ ] Write run-input records in the runs' transactions.
  Tests: a zero-chunk contextualization run records its inputs; a variant naming a different pair is rejected; the record is unchanged after reuse via applicability; the extraction input record resolves run, policy, and payload fingerprint.
- [ ] Applicability write validation, one test per clause: missing/dangling input, wrong role, cross-source scope, candidate-key mismatch, incomplete outputs on the run, split summary/chunking pair, pair not resolving to one conversion output, idempotent identical re-write, append-only (no update path).

### Reconciliation and stage rewiring

- [ ] Implement reconciliation: candidate key from current inputs + currently-resolved config; write-phase revalidation in the short writer transaction; retryable stale-plan outcome.
  Tests: input change between read and write; active run superseded between read and write — both commit nothing.
- [ ] Contextualization: run/row keys from consumed content; reuse check before per-chunk calls.
  Tests (stub client, invocation counting): byte-equivalent regenerated summary+chunking → run retained, one bound applicability row, zero per-chunk calls; changed summary text → new run; changed chunk content → new run; changed owned config → new run; regenerated upstream with identical content keeps the row key.
- [ ] Extraction: payload-based key and tri-state currentness from persisted state.
  Tests: equivalent superseded upstream → reuse with zero extractor calls; changed input text, changed input policy, changed extractor/materializer version → key changes; duplicate chunk text at distinct positions is distinct payload; input-kind flip with identical text changes the key; corpus staleness classification cost is independent of per-run upstream resolution (statement assertion).
- [ ] Staleness includes configuration: unchanged payload + changed resolved config classifies stale; equivalent reconciled upstream classifies not stale.
- [ ] Widen re-admission eligibility to "not current"; worker branch on candidate key.
  Tests: needs-reconciliation source → applicability recorded, zero extractor calls, run retained; stale source → fresh extraction supersedes; current source skipped as ineligible.
- [ ] Rebuild the console staleness derivation on applicability anti-joins; test dashboard stale count and monitor marking distinguish stale from needs-reconciliation.
- [ ] Rebuild-correlation test: ingest the same fixture into two fresh databases; internal identities differ; each logical source matches across them by source metadata and content fingerprint.

## Parallel track — environment and conversion (MUST complete before Phase 4)

- [ ] Update the uv environment (locked dependency upgrade); fix fallout; suite green.
- [ ] Switch the default model profile resolution to `gpt-5.6-luna`; confirm the config fingerprint captures the change; update env/config docs.
- [ ] Update docling to latest; review release notes for required code changes and usable features; suite green.
- [ ] Research spike: evaluate open document-intelligence pipelines (current docling incl.
  VLM options, llamaindex/liteparse as the common path with escalation, and the current field); write a preliminary-research entry in `docs/decision-record/`.
  The decision gates the pave: converter swap → its own change lands first; update-and-keep → done here.

## Phase 4 — Re-baseline & pave

- [ ] Squash Alembic history to one baseline revision matching the final ORM schema; equivalence fixtures point at it; empty-database round-trip test passes.
- [ ] Runner refuses a database stamped with a pre-baseline revision, with an error naming the rebuild path; test against a fixture DB carrying a legacy stamp.
- [ ] Bump version to `0.2.0`; update the changelog via the release tooling.
- [ ] Schema check: regenerate both OpenAPI snapshots and diff against `schemas/before/` — expected empty per `schemas/expected.md`.
- [ ] Execute the runbook: stop services; archive old DB + WAL + config + binary version; verify the archive restores; create the fresh database; start `0.2.0`.
- [ ] Smoke-validate on the console: ingest a few arbitrary inputs; confirm propagation conversion→extraction, creation events with origins in the drill-down trail, exact pending/stale counts, FTS search hits.
- [ ] Final gate: full suite (`-n auto -m "not integration_lifecycle"` plus the lifecycle marker run), all hooks over everything changed since `HEAD`, key-parsing `rg` guard clean.

## Closeout

- [ ] Sync delta specs into baseline (`sdd-sync`); while syncing, correct the stale prose in `graph-work-intake` and `pipeline-work-admission` Technical Notes/Purpose (spend-bound overclaims → creation-gate framing).
- [ ] Rebase the open `keyterm-extraction-foundation` change against the synced identity and key contracts.
- [ ] Clean `._scratch/`: remove the 2026-08-28 review record, L4 analysis/plan, retype plan, and `review-20260827/` diff packets; update the memory entries that point at them.
- [ ] Archive the change (`sdd-archive`).
