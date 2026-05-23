# Tasks: Chunk Persistence and Contextualization

> Build order within this change: graph-stage foundation (run primitive + schema) → chunk persistence + run membership → contextualization runs → run-mode entry points → stage adapter/UI on the shared runtime.
> The core groups (foundation, chunk persistence, contextualization, run-mode) are independent of the shared runtime and implementable on their own. The final group depends on the `pipeline-stage-runtime` change being implemented first, since it builds on the runtime's adapter/repository protocol and run primitive. The runtime ships no generic operator UI (deferred), so this stage builds its own operator view.
> Tests use a stub LLM client; no live model calls. Run via `uv run pytest tests/graph/`.

## Graph-stage foundation (run primitive + schema)

- [ ] Create `src/aizk/graph/` package (set `setproctitle`, structured-logging conventions matching `aizk.conversion`).
- [ ] Define the stage-run model in `src/aizk/graph/datamodel.py`: a run record (`run_id`, stage, version stamps, `input_fingerprint`, `supersedes_run_id`, `status` active|superseded) and the `record_transition`-style helper that flips run status in the same transaction as an append-only transition event. (This is the local form of the primitive `pipeline-stage-runtime` later extracts.)
- [ ] Define ORM models: `chunk` (PK content-addressed `chunk_id`; all `Chunk` fields; immutable, run-independent), `chunk_run_membership` (append-only `(run_id, chunk_id)`), `document_summary` (run-scoped; `input_fingerprint` incl. markdown hash; `summary_version`), `contextualized_chunk` (run-scoped; `chunk_id`; `context_version`; `input_fingerprint` incl. summary + neighbor identities; delta blurb only).
- [ ] Add an Alembic migration creating these tables in the conversion migration tree.
- [ ] Test `tests/graph/test_migrations.py`: migrated schema structurally equivalent to the ORM baseline (tables/columns/indexes/FKs), mirroring the `schema-migrations` equivalence test. **[schema fidelity]**

## Chunk persistence + run membership (`chunking` delta)

- [ ] Implement chunk persistence in `src/aizk/graph/persistence.py`: write each emitted chunk field unaltered; reuse an existing row on `chunk_id` collision; record append-only membership in the document's chunking run.
- [ ] Implement run supersession: opening a new chunking run for a document transitions the prior run's status to superseded (via the run transition helper); never mutate/delete prior chunk rows or memberships.
- [ ] Test `tests/graph/test_chunk_persistence.py::test_round_trip_fidelity` and `::test_full_set_in_run`: persisted chunks read back field-for-field equal; the full emitted set is present as members of the run. **[fidelity]**
- [ ] Test `::test_reinsert_reuses_row` and `::test_novel_chunk_stored_once`: re-persisting an existing `chunk_id` reuses the row unmodified; a novel `chunk_id` is stored once. **[content-addressed immutable rows]**
- [ ] Test `::test_shared_chunk_current_via_new_run`, `::test_prior_only_chunk_not_current`, `::test_new_only_chunk_current`: across a re-chunk into a new run, a shared `chunk_id` is current via membership with its row unchanged, a prior-only `chunk_id` becomes non-current via run supersession with its row intact, and a new-only `chunk_id` is current via membership; no row mutated. **[run-level supersession — shared / prior-only / new-only partition]**

## Contextualization runs (`chunk-contextualization`)

- [ ] Define an injected LLM client interface in `src/aizk/graph/llm.py` backed by `pydantic-ai`; provide a deterministic stub for tests.
- [ ] Implement the summary pass: one call per document; persist one run-scoped `document_summary` carrying the markdown-hash input fingerprint + `summary_version`; reuse the active run when inputs+version are unchanged; open a superseding run when the markdown changes.
- [ ] Implement the per-chunk contextualization pass: frame `<instructions><summary><prior_chunk><working_chunk><next_chunk>`; persist one run-scoped `contextualized_chunk` delta carrying `chunk_id` + `context_version` + the summary/neighbor input fingerprint; do not write to the `chunk` row.
- [ ] Implement reconstruct-at-use (`summary + neighbor chunks + working chunk` at the run's recorded inputs) and the contextualization on/off toggle, recording which input (raw vs contextualized) was used.
- [ ] Test `tests/graph/test_contextualization.py::test_summary_with_fingerprint_and_version`, `::test_unchanged_inputs_no_new_run`, `::test_changed_markdown_supersedes`. **[summary run — unchanged / changed-input partition]**
- [ ] Test `::test_variant_with_provenance_and_fingerprint`, `::test_changed_neighbor_supersedes_variant`, `::test_unchanged_inputs_no_duplicate_variant`. **[variant run — unchanged / changed-input partition]**
- [ ] Test `::test_source_chunk_unchanged_after_contextualization`: chunk `text`/`content_hash`/`chunk_id` equal before and after; variant stored apart. **[source chunk never modified]**
- [ ] Test `::test_variant_resolves_cross_chunk_reference`: a stub returning a reference-resolved variant is persisted and provenance-linked. **[self-contained — structural; quality dimension waived to offline eval, see design.md § Verification Waivers]**

## Run-mode entry points

- [ ] Implement a per-document processing unit and the two entry points that invoke it through the **same** write path: bulk/backfill (batched per document) and incremental (single document).
- [ ] Test `tests/graph/test_run_mode.py::test_bulk_and_incremental_same_records` and `::test_incremental_matches_bulk_shape`: the same document in each mode yields the same run records with identical provenance and input-fingerprint linkage. **[run-mode independence — bulk / incremental partition]**

## Stage adapter + operator UI on the shared runtime — depends on the pipeline-stage-runtime change

- [ ] Implement the contextualization stage adapter against the shared runtime's adapter protocol (unit-of-work: summarize a document, then contextualize its chunks), reusing the persistence/contextualization components above and the runtime's run primitive in place of the local one.
- [ ] Register the contextualization stage with the runtime's worker harness and composition root.
- [ ] Build the contextualization stage's own operator view (work-unit list/detail/retry/cancel over the runtime's event/run records); the generic UI scaffold is deferred from `pipeline-stage-runtime`, so this view is stage-owned for now.
- [ ] Test `tests/graph/test_contextualization_stage.py`: a queued contextualization work-unit runs through the runtime to completion, emits transition events, and persists the expected run, summary, and variant records (stub LLM client).

## Documentation

- [ ] Add `src/aizk/graph/README.md`: the pipeline (split → persist → summarize → contextualize), the run/dataset-version model, the stores, versioning (`splitter_version`/`summary_version`/`context_version`) and input fingerprints, run-level supersession, and the toggle.
