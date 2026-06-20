# Proposal: Graph Operator UI

## Intent

The graph stage (chunking → document summary → chunk contextualization) now persists chunks and contextualized variants, but there is no operator surface to observe it.
Two gaps:

- **No job visibility.**
  Conversion has an HTMX jobs page; the graph stage's contextualization work-units have only a JSON API, so an operator cannot monitor, filter, retry, or cancel them the way they can conversion jobs.
- **No way to see or find content.**
  There is no way to understand how a document was chunked, read a chunk's current contextualized representation, or locate a chunk without scrolling — and these are exactly the questions to ask while debugging the transformation and the knowledgebase.

This change adds an operator UI for the graph stage: a jobs-monitoring view mirroring conversion-ui, and a content **explorer** that walks the lineage source → extracted markdown → chunk → contextualized chunk with type-filtered search.
It is deliberately a **precursor to the eventual knowledge-graph explorer** — the chunk is treated as the addressable unit that mention-extraction (a later change) will hang entities and edges on — so the design favors a durable content substrate over pipeline-debugging features that belong in observability tooling.

Wireframes from the design session are saved at `.specs/changes/graph-operator-ui/mockups/` (`01-document-browser.html`, `02-search-results-paired.html`).

## Scope

Capabilities are listed in build-dependency order: `graph-jobs-ui` first (it establishes the shared graph UI infrastructure with the least new surface), then `graph-explorer-ui` (which reuses that infrastructure and adds its own search data foundation).

**In scope:**

- **`graph-jobs-ui` capability (new).**

  - An HTMX jobs page for contextualization work-units, mirroring conversion-ui: a table of `ContextualizationJob` rows with `WorkUnitStatus`, attempt count, timestamps, and error code; status + text filters across the full job set; offset pagination and sortable columns; full-page vs. `HX-Request` partial rendering.
  - Bulk **retry** and **cancel** actions over selected jobs, with an applied-vs-skipped summary.
    The graph API exposes only per-job retry/cancel, so the HTMX route loops over the per-job action **helpers** (mirroring conversion-ui's `_apply_job_*`), not a bulk endpoint — no new JSON endpoint, no OpenAPI delta.
  - A per-job **stage drill-down**: for a selected job's source, the chunking / document-summary / chunk-contextualization `PipelineRun`s (by `(stage, scope_key=str(aizk_uuid))`, with active/superseded status) plus the contextualization work-unit's lifecycle **event trail** (`PipelineEvent` has no `scope_key`; its events are work-unit lifecycle events under `stage="contextualization"`, queried by `aizk_uuid` / work-unit ref).
    So an operator sees which stage runs exist for the source and where the work-unit succeeded or failed.
  - The shared graph UI plumbing this and the explorer both use: the `graph/templates/` directory, the UI router, its mount on the **graph operator app** (`graph/api/main.py` — a separate app from the conversion app whose OpenAPI is schema-tracked, so no tracked-schema delta), and the integration-test harness.
    All UI routes (read and mutate) resolve the same request principal and sit behind the same trusted-host perimeter as the graph JSON API.

- **`graph-explorer-ui` capability (new), in two layers built foundation-first:**

  - **Search foundation.**
    A `SearchProvider` seam with a SQLite **FTS5 + `bm25()`** implementation over chunk text and contextualized text.
    The index is maintained **append-only** (a row per chunk-row creation and per variant; empty revisions index the raw chunk text), holding **all committed rows regardless of active/superseded status** — currency is decided by **query-time filtering to the active chunking-run membership and active variant run**, not by index maintenance.
    Because the index is **derived state**, the migration that creates it **backfills from all committed chunks and contextualized representations** (not only active — so a superseded-then-reactivated `chunk_id` is never missed), and a **rebuild** path reconstructs it from those source tables (for replay or corruption recovery); rows come only from committed records, never a contextualization attempt's retained memo.
    The contextualized side is sourced strictly from committed active variant-run records, so retained checkpoint-memo outputs are never indexed or shown.
    Results are ranked by document relevance (`bm25`) then ordered by within-document chunk order.
    Search is type-filterable (chunk / contextualized / either).
    Operator input is treated as **literal terms** (index query-syntax characters escaped), empty input yields empty results, and input is length-bounded so malformed queries never error the page.
  - **Explorer views.**
    A document browser whose **left spine** shows a document's chunks in **document order** (heading path, span, char count, self-contained marker) and whose **right detail panel** shows the selected chunk's current contextualized representation (resolved text, raw|contextualized toggle, the summary/neighbor inputs that fed it, and provenance).
    Document order is the active manifest's **`span_start` ascending**, not the `chunk_id` ordering that `manifest_of_run` / `chunks_of_run` return — the explorer sorts (or a helper is added) by `span_start`.
    Search returns **paired raw │ contextualized** rows with the term highlighted wherever it appears; selecting a row opens that chunk's document at its position with the detail panel populated.
    The extracted markdown is reconstructed from chunks / windowed neighbors (S3 on demand when a chunk is selected), not separately searchable.

- **`service-logging` capability (new, cross-cutting ridealong).**

  Standing up the operator UI requires a first-class way to serve it (`aizk-graph serve`, the graph operator app on its own listener so it runs alongside the conversion API).
  Adding that second graph entrypoint surfaced that logging configuration was applied per-command and unevenly — workers configured it, `serve`/`db-init` did not.
  This change centralizes logging configuration so **every** process entrypoint, in both stages' CLIs, initializes the same structured logging before doing work. (The new `aizk-graph serve` command itself is operator tooling, not a new behavioral contract beyond the shared-logging requirement.)

**Out of scope:**

- **Variant-history comparison** — comparing a chunk's contextualizations across runs/models.
  It is a temporal, experiment-tracking question better served by observability tooling (mlflow); this UI shows the current/active representation only.
- **Knowledge-graph / entity views** — mentions, entities, edges, and graph navigation.
  Mention extraction is a separate later change; this UI builds the content substrate it will extend, not the graph itself.
- **Mutation of content** — the UI is read-only over chunks/variants/runs; the only writes are the existing job retry/cancel actions.
- **Multi-term ranking purity** — single-term/phrase `bm25` ranking is exact; the append-only index's inclusion of superseded rows mildly skews multi-term corpus statistics, accepted until compaction or an active-only index is warranted.
- **Vector / embedding search** — deferred (no embeddings yet, ADR-007); the `SearchProvider` seam leaves room for it.
- **FTS index compaction** — pruning superseded rows from the FTS index rides the already-deferred contextualization compaction sweep, not this change.

## Approach

> Mechanism sandbox; contracts live in the delta specs, chosen mechanisms formalize in `design.md`.

- **Mirror conversion-ui for plumbing.**
  The graph UI is HTMX server-side like `conversion/api/routes/ui.py` + `conversion/templates/`: server-side filtering/sorting/pagination, full-page vs. `HX-Request` partial.
  Bulk retry/cancel loops over the per-job action helpers (the graph API has no bulk endpoint), mirroring conversion-ui's `_apply_job_*`.
  The stage drill-down reads stage **runs** as `PipelineRun` by `(stage, scope_key=str(aizk_uuid))` and the work-unit **events** as `PipelineEvent` by `aizk_uuid` / work-unit ref under `stage="contextualization"` (events carry no `scope_key`).
- **FTS5 append-only + read-time active filter.**
  Two FTS5 virtual tables (or one with a `kind` column) over chunk text and contextualized text, carrying `chunk_id` / `run_id` / `doc_id` as `UNINDEXED` columns.
  Rows are inserted in the existing `persist_chunks` / contextualization persist transactions (chunk row per chunk-row creation; variant row per committed variant; empty revision indexes raw text).
  The index holds **all committed rows regardless of active/superseded status** (currency is a query-time filter), sourced only from committed records — never the contextualization checkpoint memo.
  The search query drives on `MATCH`, then joins the active chunking-run manifest and active variant run to drop superseded hits.
  This avoids all supersession-sync; the cost is mildly stale `bm25` corpus statistics (a non-issue for single-term ordering), self-healed by later compaction.
  **Migration:** verify FTS5 is compiled into the app's and Litestream's SQLite (fail the migration clearly if absent); **backfill** the index from **all committed** chunks/variants (not only active); expose a **rebuild** path that reconstructs the index from the source tables for replay/recovery.
  **Input handling:** operator input is tokenized/escaped to a literal-term FTS query (no exposed `MATCH` operator syntax), empty input short-circuits to no results, and input is length-bounded so malformed queries cannot error the page.
- **Lineage queries.**
  The explorer composes existing helpers — `active_chunking_run`, manifest membership, the active summary/variant-run lookups, and `S3MarkdownSource.load` for markdown reconstruction.
  Document order is by the active manifest's `span_start` ascending; the existing `manifest_of_run` / `chunks_of_run` order by `chunk_id`, so the explorer sorts by `span_start` (or a document-order helper is added) rather than relying on that ordering.
  The "current contextualized representation" is the resolved text (`resolve_chunk_text`: the revision, or the raw chunk when the revision is empty), read from the active variant run's committed records.
- **SearchProvider seam.**
  Search is reached through a provider interface so the FTS5/bm25 implementation can later be swapped or joined by a vector provider without touching the explorer views.
