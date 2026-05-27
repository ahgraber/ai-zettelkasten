# Tasks: Mention Extraction Foundation

> Build order: mention-store foundation (run model + schema) → mention persistence → extraction (pluggable NER + spans + co-occurrence) → run-mode entry points → extraction stage/UI on the shared runtime → dataset run.
> The core groups (foundation, persistence, extraction, run-mode) depend only on the prior change's chunk/contextualization stores and are otherwise self-contained. The final group depends on the `pipeline-stage-runtime` change being implemented first.
> Tests use a deterministic stub extractor; the real spaCy / GLiNER2 models are exercised only by the offline gold-set eval. Run via `uv run pytest tests/graph/`.

## Mention-store foundation (run model + schema)

- [ ] Define ORM models in `src/aizk/graph/datamodel.py`: `mention_run` (`run_id`, `extractor_version`, `reifier_version`, `input_policy`, contextualization `input_fingerprint`, `supersedes_run_id`, `status` active|superseded), `mention` (PK `mention_id`; `run_id`; `chunk_id` FK → chunk; `surface_form`, `source_chunk_span`, `input_span`, `input_kind`, `input_ref`, `blocking_keys`, `source_occurrence_key`; no embedding column), `mention_cooccurrence` (`run_id`, `mention_id_lo`, `mention_id_hi`, `chunk_id`).
- [ ] Implement `derive_mention_id(run_id, chunk_id, source_chunk_span, surface_form)` and `derive_source_occurrence_key(chunk_id, source_chunk_span, source_anchor_text)` as deterministic, cross-process-stable hashes.
- [ ] Add an Alembic migration creating the run, mention, and co-occurrence tables in the conversion migration tree (no vector table).
- [ ] Test `tests/graph/test_mention_migrations.py`: migrated schema structurally equivalent to the ORM baseline, including the `mention.chunk_id` foreign key. **[schema fidelity, MS5 FK]**
- [ ] Test `tests/graph/test_mention_id.py`: `mention_id` is deterministic in `(run_id, chunk_id, source_chunk_span, surface_form)`; `source_occurrence_key` is deterministic in `(chunk_id, source_chunk_span, source_anchor_text)` and run-independent. **[MS2 determinism]**

## Mention persistence (`mention-store`)

- [ ] Implement `persist_mentions(session, run, mentions)` in `src/aizk/graph/mention_store.py`: append-only inserts under a run; idempotent on `mention_id`; no in-place update/delete; opening a new reification run transitions the prior run's status to superseded via the run transition helper.
- [ ] Test `tests/graph/test_mention_store.py::test_mentions_append_only` and `::test_re_reification_supersedes_and_retains`: a persisted mention is never mutated; a new run supersedes the prior and prior mentions remain. **[MS1]**
- [ ] Test `::test_run_scoped_id_distinct_occurrence_key_stable`: the same source occurrence under two runs gets distinct `mention_id`s but equal `source_occurrence_key`s; re-running within a run does not duplicate. **[MS2 — within-run / cross-run partition]**
- [ ] Test `::test_provenance_and_spans_present_no_embedding`: every mention has `surface_form`/`chunk_id`/`source_chunk_span`/`input_kind`/`input_ref`/`input_span`/`blocking_keys` populated and no embedding field. **[MS3]**
- [ ] Test `::test_verbatim_span_resolves` and `::test_resolved_reference_anchor`: a verbatim mention's `source_chunk_span` resolves to its surface form; a resolved-reference mention anchors `source_chunk_span` to the referring expression with `surface_form` the resolved form and `input_kind` contextualized. **[MS3 — verbatim / resolved-reference partition]**
- [ ] Test `::test_cooccurrence_resolvable_off_row`: a mention's co-occurrences are resolvable from the link table without a co-occurrence field on the mention row. **[MS4]**
- [ ] Test `::test_chunk_id_resolves`: a mention's `chunk_id` references an existing chunk. **[MS5]**

## Extraction (`entity-extraction`)

- [ ] Define a pluggable NER extractor interface in `src/aizk/graph/extraction.py`; implement spaCy and GLiNER2 extractors with their model artifacts **pinned as dependencies** (no runtime download); provide a deterministic stub for tests; `extractor_version` encodes extractor + model + config.
- [ ] Implement deterministic blocking-key derivation from the surface form.
- [ ] Implement per-chunk extraction within a run: select input (contextualized variant if available → `input_kind=contextualized`/`input_ref`=variant, else raw → `input_kind=raw`/`input_ref`=chunk); run NER; record `input_span` (read text) and map it back to a `source_chunk_span` raw-chunk anchor (referring-expression anchor for resolved references); emit mentions and intra-chunk co-occurrence pairs; stamp the run's versions.
- [ ] Test `tests/graph/test_extraction.py::test_mentions_under_run` and `::test_uniform_run_versions`. **[EE1]**
- [ ] Test `::test_verbatim_two_spans_coincide` and `::test_resolved_reference_maps_to_raw_anchor`. **[EE2 — verbatim / resolved-reference partition]**
- [ ] Test `::test_cooccurrence_pair_mutual`, `::test_singleton_no_cooccurrence`, `::test_cross_chunk_no_cooccurrence`. **[EE3 — pair / singleton / cross-chunk partition]**
- [ ] Test `::test_equal_surface_equal_blocking_keys` and `::test_blocking_keys_reproducible`. **[EE4]**
- [ ] Test `::test_extracts_from_variant_when_available` and `::test_falls_back_to_raw`. **[EE5 — available / absent partition]**
- [ ] Test `::test_substitute_extractor_drives_run_unchanged` and `::test_all_extractor_calls_through_single_access_point`: a deterministic substitute extractor supplied through the injected interface produces the run's mentions and co-occurrences with no change to stage logic or to record shape/spans/provenance; a recording stub observes every extractor invocation and the stage makes none outside the access point. **[EE7 — substitutable extractor]**

## Run-mode entry points

- [ ] Implement a per-chunk (grouped per-document) processing unit invoked by both bulk/backfill (batched per document) and incremental entry points through the **same** write path.
- [ ] Test `tests/graph/test_extraction_run_mode.py::test_bulk_and_incremental_same_mentions`: the same chunk extracted in each mode (same versions/inputs) yields the same mentions (equal `source_occurrence_key`s) and co-occurrence links. **[EE6 — bulk / incremental partition]**

## Extraction stage + operator UI on the shared runtime — depends on the pipeline-stage-runtime change

- [ ] Implement the extraction stage adapter against the shared runtime's adapter protocol (unit-of-work: extract mentions + co-occurrence for a chunk/document within a reification run), reusing the components above and the runtime's run primitive.
- [ ] Register the extraction stage with the runtime's worker harness and composition root.
- [ ] Build the extraction stage's own operator view (work-unit list/detail/retry/cancel over the runtime's event/run records); the generic UI scaffold is deferred from `pipeline-stage-runtime`, so this view is stage-owned for now.
- [ ] Test `tests/graph/test_extraction_stage.py`: a queued extraction work-unit runs through the runtime to completion, emits transition events, and persists the expected run, mentions, and co-occurrence links (stub extractor).

## Dataset production

- [ ] Add a CLI/entry point to run a reification over a corpus sample and capture cold-start dataset statistics (mention count, singleton rate, mentions-per-chunk, co-occurrence density) as the dataset deliverable for canonicalization calibration.
- [ ] Add `src/aizk/graph/README.md` section: the mention store, run model, `mention_id`/`source_occurrence_key` derivation, span coordinate systems (`input_span` vs `source_chunk_span`), `input_kind`/`input_ref`, the no-stored-embedding decision, and the spaCy + GLiNER2 pinned-extractor choice (techstack recorded against ADR-006 §3).
