# Tasks: Mention Extraction Foundation

> Build order: mention-store foundation (run model + schema) → mention persistence → extraction (pluggable NER + spans + co-occurrence) → run-mode entry points → extraction stage/UI on the shared runtime → dataset run.
> The core groups (foundation, persistence, extraction, run-mode) depend only on the prior change's chunk/contextualization stores and are otherwise self-contained. The final group depends on the `pipeline-stage-runtime` change being implemented first.
> Tests use a deterministic stub extractor; the real spaCy / GLiNER2 models are exercised only by the offline gold-set eval. Run via `uv run pytest tests/graph/`.

## Mention-store foundation (run model + schema)

- [ ] Define ORM models in `src/aizk/graph/datamodel.py`: `mention` (surrogate PK `mention_id`; `run_id` logical ref to `pipeline_runs`; `chunk_id` FK → chunk; `anchor_kind` `source`|`revision` with CHECK; `source_chunk_span` and `source_occurrence_key` nullable, populated iff source-anchored (CHECK); per-class partial unique indexes — source: `(run_id, chunk_id, source_chunk_span, surface_form)`, revision: `(run_id, chunk_id, surface_form)`; `surface_form`, `input_span`, `input_kind`, `input_ref`, `blocking_keys`; no embedding column), `mention_cooccurrence` (composite PK `(run_id, mention_id_lo, mention_id_hi)`; `CHECK(mention_id_lo < mention_id_hi)` canonical ordering; both endpoints FK → `mention.mention_id`; `chunk_id`). Reification runs reuse the shared `pipeline_runs` primitive — no stage-specific run table: `scope_id = str(source_id)`, extractor/reifier versions and input policy recorded in `version_stamps`.
- [ ] Implement the reification run's derivation key (deterministic over `extractor_version`, `reifier_version`, `input_policy`, and the consumed upstream run's `derivation_key`; no local surrogate ids) and `derive_source_occurrence_key(chunk_id, source_chunk_span, source_anchor_text)` as deterministic, cross-process-stable functions.
- [ ] Add an Alembic migration creating the run, mention, and co-occurrence tables in the shared `aizk.db` migration tree (no vector table).
- [ ] Test `tests/graph/test_mention_migrations.py`: migrated schema structurally equivalent to the ORM baseline, including the `mention.chunk_id` foreign key, the anchor-class CHECK constraints, the per-class partial unique indexes, and the co-occurrence pair constraints (composite PK, `lo < hi` check, endpoint FKs). **[schema fidelity, MS5 FK]**
- [ ] Test `tests/graph/test_mention_keys.py`: the reification derivation key is deterministic in its inputs, changes when any input changes, and embeds no local surrogate ids; `source_occurrence_key` is deterministic in `(chunk_id, source_chunk_span, source_anchor_text)` and run-independent. **[MS1/MS2 determinism]**

## Mention persistence (`mention-store`)

- [ ] Implement `persist_mentions(session, run, mentions)` in `src/aizk/graph/mention_store.py`: append-only inserts under a run; idempotent per anchor class (source: `(run_id, chunk_id, source_chunk_span, surface_form)`; revision: `(run_id, chunk_id, surface_form)`) and, for co-occurrence links, on the canonical pair key `(run_id, mention_id_lo, mention_id_hi)`; links inserted in canonical order with both endpoints validated to belong to the same run and chunk; `input_kind = raw` rows validated as source-anchored; no in-place update/delete; a source's reification run is opened per source via the shared run primitive (`reuse_or_record_run`, `scope_id = str(source_id)`), superseding that source's prior run.
- [ ] Test `tests/graph/test_mention_store.py::test_mentions_append_only` and `::test_re_reification_supersedes_and_retains`: a persisted mention is never mutated; a new run for a source supersedes that source's prior run and prior mentions remain; another source's active run and mentions are untouched. **[MS1 — incl. per-source isolation]**
- [ ] Test `::test_run_scoped_rows_distinct_occurrence_key_stable`: the same source occurrence under two runs yields distinct mention records but equal `source_occurrence_key`s; re-persisting within a run does not duplicate. **[MS2 — within-run / cross-run partition]**
- [ ] Test `::test_provenance_and_spans_present_no_embedding`: every mention has `surface_form`/`chunk_id`/`anchor_kind`/`input_kind`/`input_ref`/`input_span`/`blocking_keys` populated, `source_chunk_span` present iff source-anchored, and no embedding field. **[MS3]**
- [ ] Test `::test_source_anchor_resolves`, `::test_revision_only_name_persisted_without_span`, and `::test_repeated_surface_one_mention_per_occurrence`: a source-anchored mention's `source_chunk_span` resolves to its surface form; a revision-resolved name absent from the raw chunk is persisted as a revision-anchored mention with input provenance and no span; a surface form repeated in the raw chunk yields one source-anchored mention per occurrence. **[MS3 — source / revision / expansion partition]**
- [ ] Test `::test_cooccurrence_resolvable_off_row`: a mention's co-occurrences are resolvable from the link table without a co-occurrence field on the mention row. **[MS4]**
- [ ] Test `::test_cooccurrence_retry_does_not_duplicate`: re-persisting a chunk's mentions and links within the same run (a retry) leaves each unordered pair recorded exactly once, in canonical `lo < hi` order. **[MS4 — duplicate-free links]**
- [ ] Test `::test_chunk_id_resolves`: a mention's `chunk_id` references an existing chunk. **[MS5]**

## Extraction (`entity-extraction`)

- [ ] Define a pluggable NER extractor interface in `src/aizk/graph/extraction.py`; implement spaCy and GLiNER2 extractors with their model artifacts **pinned as dependencies** (no runtime download); provide a deterministic stub for tests; `extractor_version` encodes extractor + model + config.
- [ ] Implement deterministic blocking-key derivation from the surface form.
- [ ] Implement per-chunk extraction within a run: select input (contextualized variant if available → `input_kind=contextualized`/`input_ref`=variant, else raw → `input_kind=raw`/`input_ref`=chunk); run NER; record `input_span` (read text); classify each detected surface by searching the raw chunk text — one source-anchored mention per raw occurrence (each with its own `source_chunk_span`), or one revision-anchored mention (no span) when the surface does not occur; emit mentions and intra-chunk co-occurrence pairs; stamp the run's versions.
- [ ] Test `tests/graph/test_extraction.py::test_mentions_under_run` and `::test_uniform_run_versions`. **[EE1]**
- [ ] Test `::test_source_two_spans_coincide`, `::test_revision_only_name_emitted_as_revision_anchored`, and `::test_repeated_surface_expands_per_occurrence`. **[EE2 — source / revision / expansion partition]**
- [ ] Test `::test_cooccurrence_pair_mutual`, `::test_singleton_no_cooccurrence`, `::test_cross_chunk_no_cooccurrence`. **[EE3 — pair / singleton / cross-chunk partition]**
- [ ] Test `::test_equal_surface_equal_blocking_keys` and `::test_blocking_keys_reproducible`. **[EE4]**
- [ ] Test `::test_extracts_from_variant_when_available` and `::test_falls_back_to_raw`. **[EE5 — available / absent partition]**
- [ ] Test `::test_substitute_extractor_drives_run_unchanged` and `::test_all_extractor_calls_through_single_access_point`: a deterministic substitute extractor supplied through the injected interface produces the run's mentions and co-occurrences with no change to stage logic or to record shape/spans/provenance; a recording stub observes every extractor invocation and the stage makes none outside the access point. **[EE7 — substitutable extractor]**

## Run-mode entry points

- [ ] Implement a per-chunk (grouped per-document) processing unit invoked by both bulk/backfill (batched per document) and incremental entry points through the **same** write path; a document's run record and all its mention/co-occurrence rows commit in a single transaction.
- [ ] Test `tests/graph/test_mention_store.py::test_partial_failure_exposes_no_active_run`: a reification forced to fail mid-persist leaves no newly-active run for the source, keeps the prior run active, and exposes no mentions from the failed attempt. **[MS1 — atomic visibility]**
- [ ] Test `tests/graph/test_extraction_run_mode.py::test_bulk_and_incremental_same_mentions`: the same chunk extracted in each mode (same versions/inputs) yields the same mentions (equal `source_occurrence_key`s for source-anchored, equal `(chunk_id, surface_form)` for revision-anchored) and co-occurrence links. **[EE6 — bulk / incremental partition]**

## Extraction stage + operator UI on the shared runtime — depends on the pipeline-stage-runtime change

- [ ] Implement the extraction stage adapter against the shared runtime's adapter protocol (unit-of-work: extract mentions + co-occurrence for a chunk/document within a reification run), reusing the components above and the runtime's run primitive.
- [ ] Register the extraction stage with the runtime's worker harness and composition root.
- [ ] Build the extraction stage's own operator view (work-unit list/detail/retry/cancel over the runtime's event/run records); the generic UI scaffold is deferred from `pipeline-stage-runtime`, so this view is stage-owned for now.
- [ ] Test `tests/graph/test_extraction_stage.py`: a queued extraction work-unit runs through the runtime to completion, emits transition events, and persists the expected run, mentions, and co-occurrence links (stub extractor).

## Dataset production

- [ ] Add a CLI/entry point to run a reification over a corpus sample and capture cold-start dataset statistics (mention count, singleton rate, mentions-per-chunk, co-occurrence density — each partitioned by anchor class) as the dataset deliverable for canonicalization calibration.
- [ ] Add `src/aizk/graph/README.md` section: the mention store, the per-source run scoping on the shared run primitive (derivation key + `source_occurrence_key` derivation), span coordinate systems (`input_span` vs `source_chunk_span`) and the `source`|`revision` anchor classes with per-occurrence expansion, `input_kind`/`input_ref`, the no-stored-embedding decision, and the spaCy + GLiNER2 pinned-extractor choice (techstack recorded against ADR-006 §3).
