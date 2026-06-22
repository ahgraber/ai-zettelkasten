# Tasks: Identity & Provenance Foundation

> Build order: `pipeline-identity` foundation (shared derivation-key helper + grammar doc) → the `source_id`/`scope_id` rename across conversion, graph, and the run primitive → the `chunk_id` surrogate + the single Alembic migration → the breaking API/OpenAPI rename → lazy-invalidation + the human-confirmation gate → the non-requirement sweep + final verification.
> Each later group depends only on capabilities built by earlier groups. Tests run via `uv run pytest`.

## Pipeline-identity foundation

- [x] Implement a shared `derivation_key` helper that hashes **semantic inputs only** (content fingerprints, producer/prompt/model/config versions, and the upstream run's `derivation_key`) over a canonical serialization, with no database-local identifier (`run_id`, autoincrement ids, surrogate row ids) admitted as input.
- [x] Test `test_derivation_key_excludes_db_local_ids`: the same logical inputs produce an equal `derivation_key` even when surrogate/run ids differ (the portability proxy — no real Postgres needed). **[semantic-derivation-key, portable-knowledge]**
- [x] Test `test_derivation_key_propagates_upstream_change`: changing an upstream input flips the upstream `derivation_key`, which flips the downstream `derivation_key`. **[semantic-derivation-key]**
- [x] Test `test_run_level_idempotency_reuse`: re-invoking a stage for a scope whose `derivation_key` matches the active run reuses it (no new run, no duplicate). **[run-level idempotency]**
- [x] Add `docs/decision-record/` reference (or a `pipeline-identity` README section) documenting the identifier grammar: the five roles, the `_id`/`_key`/`_hash` suffix convention, the surrogate-identity rule, semantic derivation keys, and lazy invalidation — the standing reference future stages cite. **[canonical-name, suffix-convention]**

## `source_id` / `scope_id` rename (models + code)

- [ ] Rename `aizk_uuid` → `source_id` on the `Source` ORM model and every model that FKs it (`ConversionJob`, `ConversionOutput`, graph-stage rows), and `doc_id` → `source_id` on the chunk address/model.
- [ ] Rename `scope_key` → `scope_id` on the run primitive (`PipelineRun` / `record_run`) and the run/event tables.
- [ ] Update all conversion, graph, and runtime code (kwargs, queries, accessors) to the renamed fields; remove now-dead references.
- [ ] Test `test_rename_round_trip`: persisting and querying by `source_id` / `scope_id` works across conversion, graph, and the run primitive; no `aizk_uuid`/`doc_id`/`scope_key` attribute remains on the models. **[canonical-name]**

## Public API + OpenAPI rename (breaking)

- [ ] Capture the OpenAPI `before/` snapshot into `.specs/changes/identity-provenance-foundation/schemas/before/` and write `schemas/expected.md` describing the `aizk_uuid` → `source_id` rename diff.
- [ ] Rename the API surface: path parameter `GET /v1/bookmarks/{source_id}/outputs`, the job-list `source_id` query parameter, and `source_id` schema fields; regenerate the OpenAPI snapshot.
- [ ] Test `tests/conversion/test_api_source_id.py`: the bookmark-outputs route and job-list filter accept and return `source_id`; the materialize-Source flow persists the `source_id` identity while keying Source reuse on `source_ref_hash` (identity distinct from the dedup key). **[conversion-api MODIFIED]**

## `chunk_id` surrogate (chunking) + Alembic migration

- [ ] Change the `Chunk` model: `chunk_id` becomes a stable surrogate (uuid7) PK; add `UNIQUE(source_id, heading_path, ordinal, content_hash)`; keep `content_hash` as an observable column.
- [ ] Update the splitter so it no longer assigns `chunk_id`; it deterministically produces `content_hash` and the sameness-key fields.
  Persistence assigns a new surrogate for a novel sameness-key and reuses the existing surrogate when the key is already present.
- [ ] Repurpose the chunk content fingerprint: rename `derive_chunk_id` → `derive_chunk_content_key` (same body, `doc_id` → `source_id` param) — it is the sameness-key fingerprint, no longer an identity.
  Redirect the contextualization variant derivation keys (`_variant_run_derivation_key`'s chunk list and `_variant_row_derivation_key`'s working/prior/next fields) and the memo keys they feed from the surrogate `chunk_id` to `derive_chunk_content_key`, renaming the `*_chunk_id` derivation-key fields to `*_chunk_key` (the `_key` convention).
  `ContextualizedChunk.chunk_id` and `_verify_chunking_provenance` keep the surrogate — identity/provenance, not a derivation input.
  **[chunk-contextualization conformance — semantic derivation key]**
- [ ] Author the single Alembic migration (in the shared `aizk.db` tree), ordered: (1) `chunk_id` surrogate — assign a surrogate per distinct sameness-key, repoint every `chunk_id` reference (contextualization variants, chunk-run manifests, and the `graph_content_fts` content-index column) through an old→new map, add the `UNIQUE` sameness-key constraint, swap the PK; (2) `aizk_uuid` → `source_id` column + dependent FKs; (3) `scope_key` → `scope_id` column.
- [ ] Test `tests/.../test_chunk_identity.py::test_same_address_same_content_same_id`, `::test_same_address_diff_content_diff_id`, `::test_diff_address_same_content_diff_id`: the three identity scenarios hold under surrogate + sameness-key reuse. **[chunking: Chunk identity is a stable surrogate…]**
- [ ] Test `::test_re_persist_reuses_identity` and `::test_novel_chunk_stored_once`: re-persisting a chunk with an existing sameness-key reuses its surrogate; a novel key is stored exactly once. **[chunking: Chunk identities are immutable stable surrogates]**
- [ ] Test `::test_splitter_deterministic_content_hash`: the splitter produces identical `content_hash` and sameness-key fields across two invocations and two processes, and does not assign `chunk_id`. **[chunking: Splitter is a deterministic pure function]**
- [ ] Test `::test_chunk_id_no_db_local_input`: the surrogate is content/sequence-independent — the portability proxy for `chunk_id`. **[chunking surrogate, portable-knowledge]**
- [ ] Test `::test_variant_derivation_key_no_db_local_input`: the contextualization variant run/row derivation keys are invariant when chunks' surrogate `chunk_id`s differ but their content keys match — the portability proxy for the stochastic stage (mirrors `test_chunk_id_no_db_local_input`). **[chunk-contextualization conformance — portable-knowledge]**
- [ ] Test `tests/.../test_chunk_id_migration.py::test_fk_integrity_after_repoint`: after the surrogate migration, every repointed `chunk_id` reference (variants, manifests, `graph_content_fts`) resolves with no dangling reference, on a populated fixture. **[migration risk — FK-repoint write-site]**
- [ ] Test `test_orm_migration_equivalence` (via `schema-migrations`): the migrated schema is structurally equivalent to the ORM baseline after all three migration steps. **[schema fidelity]**

## Lazy invalidation + human-confirmation gate

- [ ] Implement staleness detection: a producer-version change marks prior generations logically stale (recorded version vs. current) without eager recompute; a stale-but-active generation remains usable until recomputed lazily on access or by an explicit operation.
- [ ] Implement a surface-agnostic human-confirmation gate on user-initiated reprocessing with large downstream blast radius (corpus-wide backfill; base-document edit that cascades the derivation graph): the operation does not run until explicit approval (warn + approve; no cost computed).
  Wire it into the existing reprocessing entry points.
- [ ] Test `test_version_bump_marks_stale_no_eager_recompute`: bumping a producer version marks the active generation stale but does not recompute it. **[lazy-invalidation]**
- [ ] Test `test_large_reprocessing_requires_confirmation`: a corpus-wide reprocessing op does not run until explicit confirmation is given. **[affordable-pipeline-evolution]**
- [ ] Test `test_mixed_version_corpus_valid_and_queryable`: a corpus with rows on more than one producer version is valid, each row records its version, and any version's coverage is queryable. **[lazy-invalidation]**
- [ ] Test `tests/.../test_contextualization_conformance.py`: a `context_version` bump marks contextualized variants stale without eager recompute, and a corpus-wide re-contextualization hits the confirmation gate — the `chunk-contextualization` conformance committed in `design.md`. **[conformance-not-restatement]**
- [ ] Test `::test_variant_identity_conforms`: contextualized-variant identity is a run-scoped surrogate, and re-contextualization mints new variant identities under a superseding run (the stochastic-producer branch). **[surrogate identity — stochastic partition]**

## Non-requirement sweep + final verification

- [ ] Sweep non-requirement text to `source_id`/`scope_id`: the conversion specs' Technical Notes (S3 layout `s3://…/<source_id>/`, route list, idempotency-key note), `chunk-contextualization`'s run note (`str(source_id)`), and `pipeline-stage-runtime`'s Purpose line (`scope_id`).
- [ ] Sweep the whole tree for stray `aizk_uuid` / `doc_id` / `scope_key`: code, fixtures, `caplog`/patch-target strings, notebooks, scripts, and docs; rename to `source_id`/`scope_id`.
- [ ] Run the full suite (`uv run pytest -n auto -m "not integration_lifecycle"` then `-m integration_lifecycle`) and confirm green; confirm a repo-wide search shows no remaining `aizk_uuid`/`doc_id`/`scope_key` outside intentional historical references (commit messages, this change's `> Previously` notes).
