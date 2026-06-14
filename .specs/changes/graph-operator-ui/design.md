# Design: Graph Operator UI

## Context

The conversion stage has an HTMX operator UI (`conversion/api/routes/ui.py` + `conversion/templates/`, server-side filter/sort/paginate, full-page vs. `HX-Request` partial) mounted on the conversion app (`conversion/api/main.py:create_app`, which mounts only conversion routers).
The graph operator API is a **separate app** — `graph/api/main.py:create_app` — with its own `TrustedHostMiddleware` perimeter and a router (`graph/api/routes.py`, prefix `/v1/contextualizations`) whose every route resolves a request `Principal` via `get_principal`.
It exposes per-job `list`/`get`/`retry`/`cancel` over rich persisted state — `ContextualizationJob` (work-unit, `WorkUnitStatus`), `PipelineRun` (stage runs by `(stage, scope_key=str(aizk_uuid))`, active/superseded), `PipelineEvent` (work-unit lifecycle events under `stage="contextualization"`, keyed by `aizk_uuid` / `work_unit_ref`, **no `scope_key`**), `Chunk` + `ChunkRunManifest` (chunk + `span`), `DocumentSummary`, `ContextualizedChunk`, and `S3MarkdownSource` for markdown blobs.
There is no full-text search surface; `graph_chunks.text` and `graph_contextualized_chunks.contextualized_text` are plain columns.

This change is sequenced **after** `contextualization-checkpoint-resume`, which adds an internal LLM-output memo whose spec forbids exposing retained intermediate work as committed state.
This UI must read only committed active projections.

The schema-tracked surface (`.sdd/schema-config.yaml`) is the **conversion** app's OpenAPI — a different app from the graph operator app this UI mounts on.

## Decisions

### Decision: Mount the graph UI on the graph operator app; no tracked-schema delta

**Chosen:** The graph UI is HTMX server-rendered like conversion-ui — a `graph/api/routes/ui.py` router + `graph/templates/` — mounted on the **graph operator app** (`graph/api/main.py:create_app`), alongside the existing graph JSON router and behind its existing `TrustedHostMiddleware`.
UI routes are HTML endpoints declared `include_in_schema=False`; search is a server-rendered **form-post returning an HTML partial**, not a JSON endpoint.

**Rationale:** The schema-tracked OpenAPI is the **conversion** app's, a different app; the graph UI lives on the graph app, so **the tracked OpenAPI is unchanged regardless of `include_in_schema`** — Phase-3 snapshotting is a no-op for this change.
Mounting on the graph app (not conversion) reuses the graph app's perimeter and config/lifespan and keeps the graph operator surface cohesive.
`include_in_schema=False` keeps HTML routes out of the graph app's own generated schema; the form-post search avoids adding any JSON endpoint.

**Alternatives considered:**

- Mounting the graph UI on the conversion app — rejected: the conversion app mounts only conversion routers, and folding graph routes in would both miscouple the apps and add graph paths to the tracked conversion OpenAPI.
- A JSON search endpoint the page calls — rejected: adds a schema surface and client-side wiring for no benefit over a form-post partial.

### Decision: UI routes inherit the graph API's perimeter

**Chosen:** Every graph UI route — read (jobs table, drill-down, explorer, search) and mutate (bulk retry/cancel) — resolves the same request `Principal` via `get_principal` and sits behind the graph app's `TrustedHostMiddleware`, identical to the graph JSON API.

**Rationale:** The explorer exposes document content and the bulk actions mutate jobs, so the UI must not be a weaker perimeter than the JSON API it sits beside.
Mounting on the graph app already places UI routes behind its trusted-host middleware; adding the `get_principal` dependency keeps the principal posture uniform.
This is the current trust-network model (a single deployment principal); no app-layer auth is introduced here.

### Decision: Bulk retry/cancel loops over per-job action helpers

**Chosen:** The HTMX actions route loops over the **per-job action helpers** behind the existing `retry`/`cancel` API (mirroring conversion-ui's `_apply_job_*`), applying each in its own idempotent status transition and accumulating an applied-vs-skipped summary.
No bulk JSON endpoint is added.

**Rationale:** The graph API exposes only per-job retry/cancel.
Looping over the domain helpers keeps the bulk behavior in the UI layer, reuses the existing eligibility/transition logic, and avoids a new API surface (and OpenAPI delta).
Per-job transitions stay short and idempotent, preserving the single-writer assumption.

### Decision: Stage drill-down composes runs and work-unit events separately

**Chosen:** The drill-down reads **stage runs** as `PipelineRun` filtered by `(stage ∈ {chunking, document_summary, chunk_contextualization}, scope_key=str(aizk_uuid))`, showing each run's active/superseded status; and the **work-unit event trail** as `PipelineEvent` filtered by `aizk_uuid` (and/or `work_unit_ref` = job id) under `stage="contextualization"`.
A stage with no run row for the source renders as absent.

**Rationale:** `PipelineEvent` has no `scope_key` and its graph events are _work-unit_ lifecycle events, not per-stage-run events — so "runs by `scope_key` + events by `aizk_uuid`" is the only faithful composition.
It needs no new event contract: "which stages ran" comes from run existence, "where it failed" from the work-unit event trail.

### Decision: FTS5 schema — one discriminated virtual table, append-only

**Chosen:** A single FTS5 virtual table `graph_content_fts(text, kind UNINDEXED, chunk_id UNINDEXED, run_id UNINDEXED, doc_id UNINDEXED)` with `kind ∈ {chunk, contextualized}`.
Rows are inserted append-only inside the existing write transactions: one chunk row per chunk-row **creation** in `persist_chunks` (a reused `chunk_id` is not re-created, so it is already indexed from its first creation), one contextualized row per variant in the contextualization persist, and an empty revision indexes the raw chunk text.
**All committed chunk and variant rows are indexed regardless of active/superseded status** — currency is a query-time filter, not an index property — but inserts come only from committed records, never the checkpoint memo.

**Rationale:** One table with a `kind` discriminator avoids two near-identical tables/migrations; `UNINDEXED` columns let the query filter and label without a side join.
Indexing every committed row (not just the currently-active generation) is what makes a `chunk_id` that is superseded now but reused in a later active manifest still searchable — because `persist_chunks` reuses the row without re-inserting, an active-only index would never gain a row for it.
Append-only inserts ride existing transactions (single-writer preserved) and need no supersession hook; sourcing from the committed persist path structurally guarantees the memo is never indexed.

### Decision: FTS5 query — MATCH then active-membership filter, ranked bm25 then span order

**Chosen:** Search drives on `graph_content_fts MATCH ?`, then joins to the active chunking-run manifest (chunk membership) and the active variant run (active contextualized rows) to drop superseded hits, honoring the type filter via `kind`.
Matching rows are **aggregated by `chunk_id` into one result per chunk**, carrying per-side match flags (raw and/or contextualized) for highlighting.
Each chunk's score is the **best (minimum) `bm25()`** across its matching rows; a document's score is the best (minimum) score across its matching chunks.
Documents are ordered by score **ascending** (SQLite `bm25()` returns lower = more relevant), and within a document chunks are ordered by **`span_start` ascending**.

**Rationale:** Index-everything + read-time filter avoids supersession sync; the filter uses helpers we already have.
Aggregating by `chunk_id` is required because one chunk has two FTS rows (`kind=chunk`, `kind=contextualized`) that can both match — always so for a self-contained chunk, whose contextualized row indexes the raw text — and the UI shows one paired result per chunk; the per-side flags drive which side is highlighted.
SQLite `bm25()` scores lower-is-better, so ordering is ascending, not descending; "best score" (min) represents a document by its single most relevant chunk rather than rewarding many weak matches.
Document order within a result is `span_start` (true reading order), **not** the `chunk_id` order that `manifest_of_run`/`chunks_of_run` return — a dedicated document-order read (sort by manifest `span_start`) is used in both search and the spine.
Caveat: superseded rows linger in the index, so `bm25` corpus stats are mildly stale — no effect on single-term ordering (IDF constant), minor for multi-term, healed by the deferred compaction.

### Decision: The FTS index is rebuildable derived state

**Chosen:** The migration that creates the table **backfills** it from **all committed `graph_chunks` and `graph_contextualized_chunks` rows** (not only the currently-active generation), and a **rebuild** routine reconstructs it from those source tables (for replay or corruption recovery).
The migration **verifies FTS5 is available** in the SQLite build and fails clearly if not.

**Rationale:** Per the project's derived-state pattern, model/config-derived state must be replayable.
Append-only-on-write alone would leave pre-existing data unsearchable; backfilling **all** committed rows (not just active) closes the superseded-then-reactivated gap noted above; backfill + rebuild make the index a faithful, regenerable projection.
FTS5 is a compile-time SQLite feature; a clear migration-time check prevents a silent broken index in environments (including Litestream's SQLite) lacking it.

### Decision: Search input is escaped to a literal-term FTS query

**Chosen:** Operator input is tokenized and escaped into a literal-term FTS5 query (each token wrapped/quoted so `MATCH` operator characters — `"`, `*`, `-`, `AND/OR/NEAR` — are matched literally, not interpreted).
Empty/whitespace input short-circuits to an empty result set without querying; input is length-bounded.
Highlighting marks the operator's tokens.

**Rationale:** Raw input into `MATCH` is a query language and can error or mis-behave even with bound SQL parameters.
Treating input as literal terms is the safe, predictable behavior for an operator search and keeps malformed input from erroring the page (an external-boundary validation contract).

### Decision: Search behind a `SearchProvider` seam

**Chosen:** Search is reached through a `SearchProvider` protocol (query + type filter → ranked results); the FTS5/`bm25` implementation is one provider.
The explorer and jobs views depend on the protocol, not the implementation.

**Rationale:** Keeps the BM25→vector evolution a backend swap with no UI change, matching the modularity the design session called for. (Search is not split into its own capability, per the packaging decision; the seam is an internal boundary.)

### Decision: Lineage and "current representation" from committed active records only

**Chosen:** The explorer composes `active_chunking_run` + manifest membership (ordered by `span_start`), the active summary/variant-run lookups, and `S3MarkdownSource.load` for on-demand markdown reconstruction.
The "current contextualized representation" is `resolve_chunk_text` over the active variant run's committed `ContextualizedChunk` (the revision, or the raw chunk when the revision is empty).
A source with no active variant run shows raw chunks but no contextualized representation.

**Rationale:** Reuses existing helpers; sourcing from committed active records (never the memo) satisfies the predecessor's "intermediate work never observable" contract and the project rule that operator surfaces read project-owned projections, not internal processing state.

## Architecture

```text
graph operator app (graph/api/main.py create_app)  — separate app; TrustedHostMiddleware + get_principal
├── graph JSON API (graph/api/routes.py, /v1/contextualizations)
└── graph UI router (graph/api/routes/ui.py, include_in_schema=False, same principal) ── graph/templates/
        │   [conversion app + its tracked OpenAPI are a DIFFERENT app — untouched]
        ├── /ui/graph/jobs                 jobs table + filters/sort/paginate (HX partial)
        │     bulk actions → loop per-job retry/cancel helpers → applied/skipped summary
        │     drill-down → PipelineRun by (stage, scope_key)  +  PipelineEvent by aizk_uuid
        │
        └── /ui/graph/explorer             document spine + detail panel; search (form-post → HX partial)
              search → SearchProvider(FTS5/bm25)
                         escape input → MATCH → filter active manifest + active variant run
                         → aggregate by chunk_id (per-side flags) → bm25 ASC (lower=better), span_start asc
              spine    → active manifest chunks ordered by span_start
              detail   → resolve_chunk_text(active ContextualizedChunk)  + provenance
              markdown → S3MarkdownSource.load (on chunk select)

write path (unchanged transactions):
  persist_chunks            → INSERT graph_content_fts(kind=chunk, …)         [per chunk-row creation]
  contextualization persist → INSERT graph_content_fts(kind=contextualized)   [per committed variant]
  migration                 → create table + backfill ALL committed rows + (rebuild routine); verify FTS5
```

## Risks

- **FTS5 not compiled into the SQLite build (app or Litestream).**
  Mitigation: migration-time availability check that fails clearly; documented as a deployment prerequisite.
- **Index diverges from source tables** (a missed insert, partial write, corruption).
  Mitigation: the rebuild routine reconstructs from source tables; the index is treated as regenerable derived state, not authoritative.
- **`span_start` ordering assumed but helpers return `chunk_id` order.**
  Mitigation: explicit `span_start` sort in the spine and search, covered by an ordering test; do not rely on `manifest_of_run`/`chunks_of_run` ordering.
- **Stale `bm25` corpus statistics from superseded rows.**
  Mitigation: no effect on single-term ordering; minor for multi-term; healed by the deferred compaction sweep.
  Documented, not solved here.
- **Search-input injection / `MATCH` errors.**
  Mitigation: literal-term escaping + empty/length guards, covered by a malformed-input test.
- **Exposing checkpoint-memo state.**
  Mitigation: all reads and index inserts are sourced from committed active records; covered by a test that retained intermediate outputs never appear in search/explorer/jobs.
