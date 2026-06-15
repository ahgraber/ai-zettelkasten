# Tasks: Graph Operator UI

> Ordered by build dependency: shared UI infra + perimeter → jobs page → search data foundation → search provider → explorer views. Tests are paired with the unit they exercise. UI tests mirror the conversion-ui `TestClient` HTML-assertion harness; search/data tests run against a seeded SQLite test DB. Run tests with `uv run pytest tests/` (and `uv run pytest tests/graph/...` for the new suites); delegate to the user only on a real sandbox/permission error (`uv sync`, `.env`). FTS5 availability is confirmed by running the search tests.

## Shared graph UI infrastructure and perimeter (graph-jobs-ui)

- [x] Add a graph UI router (`graph/api/routes/ui.py`) and a `graph/templates/` directory; mount the router on the graph operator app (`graph/api/main.py:create_app`) with `include_in_schema=False`, behind its existing `TrustedHostMiddleware`.
- [x] Apply the graph API's principal dependency (`get_principal`) to every UI route (read and mutate).
- [x] Establish the graph UI integration-test harness mirroring `tests/conversion/integration/test_ui_jobs.py` (TestClient over `graph` `create_app`, `app.state.config` override, hermetic — no `.env`).
- [x] Extend the graph test schema/fixtures (`tests/graph/conftest.py`) to include and seed `Source` and `ConversionOutput`, since the jobs table joins `Source` and the explorer resolves markdown via `ConversionOutput`; the current setup seeds only graph/pipeline tables.
- [x] Test: a graph UI route is rejected for a `Host` outside the trusted-host allowlist, matching the graph API's trusted-host behavior. (perimeter — trusted-host scenario)
- [x] Test: a graph UI route resolves the same principal the graph API requires (e.g. behaves identically when the principal is absent/present). (perimeter — principal scenario)
- [x] Confirm the graph UI router is absent from the **conversion** app's OpenAPI (`aizk.conversion.api.main:create_app().openapi()`), evidencing no tracked-schema delta. (schema no-op check)

## Contextualization jobs page (graph-jobs-ui)

- [x] Implement the jobs table route: query `ContextualizationJob` joined to `Source` (via `aizk_uuid`), render columns (job id, status, attempts, queued/started/finished, error code, title); full-page vs. `HX-Request` partial.
- [x] Implement title resolution: `Source.title` when non-`NULL`, else the source `aizk_uuid`.
- [x] Implement status filter + text search across the full job set (job id, `aizk_uuid`, source title, `conversion_output` id), plus offset pagination and column sorting.
- [x] Extract shared `_apply_retry(session, job)` / `_apply_cancel(session, job)` helpers from the inline logic in `graph/api/routes.py`'s `retry_job`/`cancel_job` handlers; repoint the JSON routes at the helpers so the existing JSON API behavior is unchanged.
- [x] Test: the existing graph JSON API retry/cancel tests still pass after the helper extraction. (refactor regression)
- [x] Implement the bulk actions route: loop over the shared `_apply_retry` / `_apply_cancel` helpers, accumulating an applied-vs-skipped summary; no bulk endpoint.
- [x] Implement the stage drill-down: stage runs via `PipelineRun` by `(stage ∈ {chunking, document_summary, chunk_contextualization}, scope_key=str(aizk_uuid))` with active/superseded status, plus the work-unit event trail via `PipelineEvent` by `aizk_uuid`/work-unit ref under `stage="contextualization"`; a stage with no run renders absent.
- [x] Test: the jobs table renders with all columns on load. (display table)
- [x] Test: title shows the enriched `Source.title` when present, and falls back to `aizk_uuid` when `Source.title` is `NULL`. (display table — both title partitions)
- [x] Test: a status filter returns matching jobs from beyond the current page and excludes other statuses. (filter/search)
- [x] Test: search by source title finds the job; a non-matching term renders the empty state. (filter/search)
- [x] Test: bulk **retry** returns selected eligible jobs to queued with a summary. (bulk — retry write-site)
- [x] Test: bulk **cancel** attempts cancellation with a summary. (bulk — cancel write-site)
- [x] Test: a mixed-eligibility bulk action distinguishes applied from skipped and alters no ineligible job. (bulk — mixed eligibility)
- [x] Test: a completed job's drill-down shows all three stage runs with status plus the work-unit event trail ending in succeeded. (drill-down — completed)
- [x] Test: a job chunked-but-not-contextualized shows the chunking run present, the contextualization run absent, and the work-unit failure event surfaced. (drill-down — failed-mid-stage)

## Search data foundation (graph-explorer-ui)

- [x] Add the FTS5 migration: create `graph_content_fts(text, kind UNINDEXED, chunk_id UNINDEXED, run_id UNINDEXED, doc_id UNINDEXED)`; include `downgrade`.
- [x] Add a migration-time FTS5 availability check that fails clearly if FTS5 is not compiled into the SQLite build.
- [x] Backfill the index from **all committed** `graph_chunks` (kind=chunk) and `graph_contextualized_chunks` (kind=contextualized; empty revision indexes the raw chunk text) — not only the active generation.
- [x] Implement a rebuild routine that reconstructs `graph_content_fts` from the source tables (replay / corruption recovery).
- [x] Add the chunk FTS insert in `persist_chunks` — one row per chunk-row **creation** (reused `chunk_id` is not re-created, so not re-inserted), in the existing transaction.
- [x] Add the contextualized FTS insert in the contextualization persist — one row per committed variant (empty revision indexes raw text), in the existing transaction, sourced only from committed records (never the memo).
- [x] Update the graph test schema setup (`tests/graph/conftest.py`) to create the `graph_content_fts` virtual table (via the migration's DDL or by running migrations) — `SQLModel.metadata.create_all` cannot create a virtual table, so without this the new FTS inserts in `persist_chunks` / contextualization persist break every existing graph persistence test.
- [x] Test: the migration upgrades/downgrades cleanly on a scratch DB, and the availability check fails clearly when FTS5 is unavailable. (availability + migration)
- [x] Test: content persisted **before** the index existed is searchable after backfill. (searchability — pre-existing)
- [x] Test: a `chunk_id` not active at backfill but reused in a later active run is searchable. (searchability — superseded-then-reactivated; backfill-all write-site)
- [x] Test: rebuilding the index from source tables reproduces the same searchable content. (rebuild)
- [x] Test: a newly persisted chunk is searchable. (chunk-insert write-site)
- [x] Test: a newly persisted variant is searchable, and a self-contained chunk (empty revision) is searchable by its raw text. (variant-insert write-site + empty-as-raw)

## Search provider (graph-explorer-ui)

- [x] Define the `SearchProvider` protocol (query + type filter → ranked, per-chunk results) and an FTS5/`bm25` implementation; the explorer depends on the protocol.
- [x] Implement input escaping: tokenize/escape operator input into a literal-term FTS query; empty/whitespace short-circuits to no results; bound input length.
- [x] Implement the query: `MATCH` → join active chunking-run manifest + active variant run (drop superseded) → honor type filter via `kind` → aggregate by `chunk_id` into one result with per-side match flags → order documents by best (min) `bm25()` ascending, chunks within a document by `span_start` ascending.
- [x] Test: a superseded chunk is not returned; the active chunk is. (active-generation only — query active-filter write-site)
- [x] Test: a contextualized-only term is found under "contextualized"/"either" and not "chunk"; a raw-only term is found under "chunk"/"either" and not "contextualized"; a self-contained chunk matches under "contextualized" by its raw text. (type filter — three partitions)
- [x] Test: documents are ordered most-relevant-first and chunks within a document follow `span_start` order. (ranking — bm25-asc + doc-min + span_start write-site)
- [x] Test: a chunk matching on both sides yields a single result carrying both per-side flags. (dedup — aggregate-by-chunk_id write-site)
- [x] Test: an empty query yields an empty result set (no error, not the whole corpus); input with query-syntax characters (`"`, `*`, boolean operators) is matched literally and never errors. (input handling — escaping write-site)
- [x] Test: a source mid-contextualization (revisions retained, no active variant run) contributes no contextualized content to search; retained intermediate revisions never appear. (memo exclusion — search surface)

## Explorer views (graph-explorer-ui)

- [ ] Implement the document browser route: left spine lists the active chunking run's chunks ordered by manifest `span_start` ascending (heading path, span, char count, self-contained marker); right detail panel; full-page vs. `HX-Request` partial.
- [ ] Implement the detail panel: `resolve_chunk_text` over the active variant run's committed `ContextualizedChunk` (revision, or raw when empty), shown distinct from the raw chunk, with provenance (variant run/version/model + lineage to summary, chunking generation, source markdown) and on-demand markdown reconstruction via `S3MarkdownSource.load`.
- [ ] Implement the search-results view (form-post → HTML partial): paired raw │ contextualized rows with the term highlighted on whichever side(s) matched (per-side flags), one row per chunk; selecting a row opens the document browser at that chunk with the detail panel populated.
- [ ] Add the explicit `span_start`-ordered document-order read used by the spine and search (do not rely on `manifest_of_run`/`chunks_of_run` `chunk_id` ordering).
- [ ] Add an explorer test fixture that injects a fake `BlobReader` (and seeds the corresponding `ConversionOutput`) so markdown reconstruction via `S3MarkdownSource(engine, blob_reader)` is exercised without real S3.
- [ ] Test: a document's chunks render in `span_start` order with heading path, span, char count, and the self-contained chunk marked. (document browser spine — span_start write-site)
- [ ] Test: a chunk with a non-empty revision shows the revision distinct from raw with provenance; a self-contained chunk shows the raw text marked self-contained with the same provenance lineage. (detail panel — both revision partitions)
- [ ] Test: selecting a search result opens its document at the chunk with the detail panel showing its contextualized representation. (select-opens-document)
- [ ] Test: a contextualized-only match is highlighted on the contextualized side only; a both-sides match renders one result highlighted on both. (paired-results highlight — view write-site)
- [ ] Test: in the explorer, a source mid-contextualization shows its raw chunks but no contextualized representation, and no retained intermediate revision appears. (memo exclusion — explorer surface)
