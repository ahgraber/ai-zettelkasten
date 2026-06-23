# aizk.graph

Persistence and contextualization for converted Markdown artifacts.

Sits between the pure [`aizk.chunking`](../chunking/README.md) splitter and the (separate) mention-extraction stage: it durably and idempotently persists the splitter's chunks, summarizes each document, and rewrites each chunk into a self-contained passage — turning splitter output into a replayable substrate rather than recomputed-and-discarded text.

## The pipeline

One per-document write path, run as a worker-driven stage on the shared [`aizk.pipeline`](../pipeline/README.md) runtime:

```text
conversion Markdown artifact (fetched by conversion_output_id locator)
        │
        ▼
  aizk.chunking.split()                    ── pure, reused, unchanged
        │
        ├── persist_chunks   → chunking run + chunk identities + manifest + input
        ├── summarize_document → summary run + document_summary
        └── contextualize_chunks → variant run + one contextualized_chunk per chunk
```

`process_document` runs the whole path for one work-unit; chunk persistence and contextualization are **one required pipeline**, not separately-gated steps.

## Source-scoping by `source_id`

Every run is scoped by the **durable source identity** (`scope_id = str(source_id)`), never by a per-conversion artifact id.
Re-converting the same source supersedes within one scope rather than forking a parallel current generation.
The locator (`conversion_output_id`) is used only to fetch the Markdown; it is never a scope and never a derivation input.

## Stable-identity chunk store

Facts are split by what they are _about_, so a content-addressed `chunk` row stays honest across every generation that re-emits it:

- **`graph_chunks`** — content-addressed, immutable, run-independent identities carrying **stable facts only**: `chunk_id`, `content_hash`, `source_id` (`= str(source_id)`), `heading_path`, `ordinal`, `text`, `char_count`.
  An unchanged chunk keeps one row across re-chunks; persisting an existing `chunk_id` reuses it (and rejects a colliding id whose stable facts differ).
- **`graph_chunk_run_inputs`** — one row per chunking run: what it _consumed_ (the `conversion_output_id` locator + the `markdown_hash_xx64` that verifies it).
- **`graph_chunk_run_manifest`** — append-only `(run_id, chunk_id, span)`: what a run _produced_, and where each chunk sat in _that generation's_ markdown.

The generation-varying facts — the markdown hash, the `splitter_version`, and each chunk's `span` — live on the run/input/manifest, **not** on the shared identity (storing them there would be a first-writer lie: a re-emitted chunk would report whichever generation wrote the row first).
Round-trip fidelity of the emitted `Chunk` is reconstructed by joining `chunk ⋈ chunk_run_manifest ⋈ chunk_run_input ⋈ chunk_run` (`chunks_of_run`).

## Run / dataset-version model

A **run** is a [`PipelineRun`](../pipeline/run.py) keyed `(stage, scope_id)` with a `derivation_key`, version stamps, `supersedes_run_id`, and `status ∈ {active, superseded}`.
This stage records three run kinds per document, each superseding independently:

| Stage                     | `derivation_key` (reuse/supersede signal)                                                                                                | Output                               |
| ------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------ |
| `chunking`                | `markdown_hash_xx64` + `splitter_version`                                                                                                | chunk identities + manifest + input  |
| `document_summary`        | markdown hash + `summary_version` + summary-prompt hash + model profile                                                                  | one `document_summary`               |
| `chunk_contextualization` | summary identity + ordered `chunk_id`s + **`splitter_version`** + 2p/1n window + context-prompt hash + model profile + `context_version` | one `contextualized_chunk` per chunk |

The variant stores the model's **self-contained revision** (or an empty string when the chunk is already self-contained); the raw chunk row is never written and stays the cited, source-faithful unit.

### Versioning and derivation keys vs. locator provenance (the no-surrogate rule)

- **Version stamps** — `splitter_version` / `summary_version` / `context_version` — bump whenever a pass's observable output changes for unchanged inputs; each participates in its run's derivation key, so a bump supersedes even when source content is unchanged.
- A **`derivation_key`** decides reuse vs. supersession: idempotency means "unchanged inputs **and** version."
  Re-running an unchanged document reuses every active run and produces no duplicate row.
- **Locator provenance is kept _out_ of derivation keys** (the no-surrogate rule): `conversion_output_id`, `summary_run_id`, and `chunking_run_id` are retrieval pointers recorded alongside the row, never derivation inputs — so a key is reproducible across databases with different inserted row ids.

### Run-level supersession

Invalidation is expressed _only_ as the one blessed mutation: a run's `status` transition `active → superseded` (a bare pointer flip, not evented).
Rows are immutable; prior identities, manifests, inputs, summaries, and variants are never edited or deleted.
At most one run per `(stage, scope_id)` is active; a `chunk_id` is current iff it is in the active chunking run's manifest. (Compaction of superseded runs is the deferred `artifact-compaction-retention` change, not a mutation.)

## Backward-trace chain

A persisted variant resolves backward, one hash-verifiable edge at a time, to the source it was built from:

```text
contextualized_chunk
  → chunking_run_id            (the exact generation it read)
  → chunk_run_manifest(chunk_id, span) + graph_chunks.text   (the raw chunk + where it sat)
  → chunk_run_input(markdown_hash_xx64, conversion_output_id) (the source markdown, retrievable + verifiable)
  → source_id                  (the durable source the whole chain belongs to)
        ╲→ summary_run_id      (the document summary it used)
```

## Contextualization toggle

`resolve_chunk_text(working_text, *, contextualized_text, contextualization_enabled)` selects the raw vs. revised text at use time and records which input was used (`ContextSource.RAW` / `CONTEXTUALIZED`), enabling a downstream raw-vs-contextualized comparison.
An empty revision means "already self-contained": the consumed text is the raw chunk unchanged.
The toggle is a consumption/eval lever — and an enqueue lever for bounding the first backfill — never a per-unit branch that emits chunks without variants.

## Public surface

Domain (no commit; the caller owns the transaction):

- `persist_chunks(...) -> PipelineRun`, `chunks_of_run`, `current_chunk_ids`, `active_chunking_run`, `manifest_of_run`, `run_input`, `reconstruct_chunk`.
- `summarize_document(...) -> DocumentSummary`, `contextualize_chunks(...) -> list[ContextualizedChunk]`, `resolve_chunk_text(...)`.
- ORM: `Chunk`, `ChunkRunInput`, `ChunkRunManifest`, `DocumentSummary`, `ContextualizedChunk`, `ContextualizationJob`.
- LLM seam: `LLMClient` (one `generate(prompt)` access point), `PydanticAILLMClient`, `StubLLMClient`.

Unit-of-work and orchestration:

- `process_document(session, client, job, markdown_source) -> ProcessResult` — the single write path.
- `enqueue_document` / `enqueue_backfill` (domain) and `enqueue_output` / `enqueue_backfill_outputs` (resolve `conversion_output_id → ConversionOutput.source_id`).
  Both modes dedupe on `idempotency_key` and feed the one write path, so the produced records are run-mode-independent.
- `ContextualizationStageHandler` — the runtime `StageHandler` (claim/execute-in-own-transaction/finalize/recover/cancel; `map_result` classifies `ValueError` → permanent, other exceptions → retryable, success → succeeded; in-process, single-writer concurrency).
- `MarkdownSource` / `S3MarkdownSource` (over a `BlobReader`), `ContextualizationConfig`, `build_llm_client`, `run_graph_worker`.

Operator surface (`aizk.graph.api`): the JSON API (`GET /v1/contextualizations` (+ status filter), `GET /{id}`, `POST /{id}/retry`, `POST /{id}/cancel`) and an HTML operator UI (`/ui/graph/jobs` jobs monitor, `/ui/graph/explorer` content explorer).

The `aizk-graph` console script (or `python -m aizk.graph.cli`) has two commands:

- `aizk-graph worker` — the per-document write path (split → summarize → contextualize → persist).
- `aizk-graph serve` — the operator API + UI over uvicorn, on its own listener (default `0.0.0.0:8001`) so it runs alongside the conversion API.

Neither command manages Litestream: the graph stage reuses the conversion database, whose replication is owned by the conversion service (only the process matching `litestream_start_role` replicates).
Migrations run on `worker` startup or via `aizk-conversion db-init` over the shared Alembic tree; `serve` does not migrate.

## Configuration

The graph stage resolves the shared database URL from `DatabaseConfig` (the stage-independent `aizk.db` foundation) and reuses the conversion service's `ConversionConfig` for shared S3/application settings.
Its own settings live under `AIZK_GRAPH__CONTEXTUALIZATION__*` ([`config.py`](config.py)): the OpenAI-compatible model endpoint triple (`LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`), the worker's lease/retry knobs, and the operator API listener (`OPERATOR_API_HOST` / `OPERATOR_API_PORT` / `OPERATOR_API_RELOAD`).
The worker refuses to start when the model endpoint is not fully configured; `serve` needs only the shared database.

## References

- Delta specs: `.specs/changes/chunk-persistence-contextualization/specs/{chunking,chunk-contextualization}/spec.md` (synced into the baseline after the change lands).
- Design decisions: [`design.md`](../../../.specs/changes/chunk-persistence-contextualization/design.md); ADR-003 (database), ADR-004 (model provider), ADR-005 (pipeline pattern), ADR-006 (repairability).
- Shared runtime: [`aizk.pipeline`](../pipeline/README.md).
