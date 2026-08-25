# aizk.graph

Persistence, contextualization, and entity-mention extraction for converted Markdown artifacts.

This package sits downstream of the pure [`aizk.chunking`](../chunking/README.md) splitter and turns its output into a durable, replayable substrate: it persists the chunks, summarizes each document, rewrites each chunk into a self-contained passage, and extracts the entity mentions the graph is later built from.
Nothing is recomputed-and-discarded — every stage writes rows you can read back, trace to their source, and reproduce.

## The pipeline

One per-document write path, run as a worker-driven stage on the shared [`aizk.pipeline`](../pipeline/README.md) runtime:

```text
conversion Markdown artifact  (fetched by its conversion_output_id locator)
        │
        ▼
  aizk.chunking.split()                     ── pure, reused, unchanged
        │
        ├── persist_chunks       → chunk identities + manifest + input
        ├── summarize_document   → one document summary
        └── contextualize_chunks → one self-contained variant per chunk
        │
        ▼
  extract_mentions  (a separate downstream stage — its own worker + run)
        └── mention store        → entity mentions + co-occurrences
```

`process_document` runs the whole top path for one work-unit; chunk persistence, summary, and contextualization are **one required pipeline**, not separately-gated steps.
Mention extraction is a **distinct stage** — its own worker, its own run, run once variants exist — not another step in that path.

## How work reaches a stage

A stage's work-units are created by **admission**: each stage declares, over current upstream state, the set of work that should exist at it but has no work-unit, and a pass creates exactly that set through the stage's own enqueue.
Admission is a query, not an event — nothing pushes work into the graph — so a unit that failed to be created is simply created on the next pass, and adding a stage later pulls the existing corpus through by declaring what it consumes rather than by a one-off script.
It is distinct from the runtime's _discovery_, which selects already-queued units to claim.

| Stage             | pending exactly when                                               | stale                                                              |
| ----------------- | ------------------------------------------------------------------ | ------------------------------------------------------------------ |
| contextualization | the source's newest `ConversionOutput` has no work-unit            | n/a — a re-converted source becomes pending again                  |
| extraction        | the source has an active chunking run and no work-unit, any status | its active extraction run consumed upstream state since superseded |

Extraction's work-unit is keyed by the source alone, so a completed source that falls behind is **stale, not pending**: nothing re-admits it automatically, and an operator re-extracts it through the console's `Re-extract` action (see below).

Three surfaces create work, all through the same two enqueue primitives:

- **Automatic admission** — a loop in each stage's existing worker process, on `ADMISSION_INTERVAL_SECONDS`.
  Off unless enabled per stage, so switching the flow on is a deliberate act.
- **Intake** — `POST /v1/contextualizations` (a conversion-output reference) and `POST /v1/extractions` (a source identity) on the graph service, mirroring the conversion service's job submission.
- **Bulk commands** — `aizk-graph backfill` and `aizk-graph extraction-backfill`.

**Capacity sits at the enqueue seam**, not in front of any one caller: a stage may declare a limit over its _actionable backlog_ (units queued, plus failures awaiting retry), and every path is subject to it with no bypass.
A single enqueue is refused at the limit; a bulk enqueue truncates to the batch's remaining headroom and logs the remainder; a request resolving to an existing work-unit is returned rather than refused, since reusing a unit adds no work.
Each surface maps the refusal its own way — intake answers `503` with a `Retry-After` header (the conversion service's shape), an admission pass stops and leaves the rest pending, a command exits non-zero.
Unset (`0`) declares no limit.

## How we slice identity and provenance

Everything below rests on a few deliberate choices about _what names a thing_ and _how a thing points back to what it came from_.
A user needs these because they decide what stays stable when you re-run, and what you can trust when you read a row back.

**Source is the durable identity.**
A _source_ is the thing a document belongs to across re-conversions; its id is `source_id`.
Every run is scoped to it (`scope_id = str(source_id)`), never to a single conversion artifact.
Re-converting the same source **supersedes** within that one scope rather than forking a parallel copy.
The `conversion_output_id` is only a _locator_ — how we fetch the Markdown — never a scope and never an input to identity.

**A run is the unit of versioning.**
A _run_ is one execution of a stage for one source, keyed `(stage, source)`.
Each run carries a **derivation key**: a fingerprint of everything that would change its output — the input content plus a **version stamp**.
The key decides **reuse vs. redo**: re-running with an unchanged key is a no-op that reuses the active run; a changed key opens a new run that supersedes the old one.
_Idempotent_ here means exactly that — "same inputs **and** same version ⇒ same rows, no duplicates."
At most one run per scope is **active**; the rest are **superseded** (marked inactive, never deleted).
A corpus is the **union of every source's active run**.

**Identity is a surrogate; sameness is a separate key.**
The id of a chunk (`chunk_id`) or a mention (`mention_id`) is a **surrogate** — an opaque UUID assigned when the row is written, _not_ computed from its content.
This is deliberate: a content-derived id would churn every time the content shifted, and could collide across runs or fail to survive a database migration.
Recognizing "this is the same chunk (or occurrence) as last time" is a _separate_ concern, handled by a **reuse key** (per store, below) that never becomes the identity.

**Provenance is kept out of identity.**
Retrieval pointers — `conversion_output_id`, `summary_run_id`, `chunking_run_id` — are recorded _alongside_ a row so you can navigate, but they are **not** part of its derivation key.
Two databases that inserted the same logical rows in a different order still compute the same keys.
Provenance you can _verify_ (not just follow) is carried by content hashes, so every artifact resolves backward to its source one checkable edge at a time:

```text
contextualized_chunk / mention
  → chunking_run_id                                (the exact generation it read)
  → manifest(chunk_id, span) + chunk text          (the raw chunk + where it sat)
  → run_input(markdown_hash, conversion_output_id)  (the source markdown, retrievable + hash-verifiable)
  → source_id                                      (the durable source the whole chain belongs to)
        ╲→ summary_run_id                          (the document summary it used)
```

### The runs, concretely

Each stage records its own run kind, superseding independently:

| Stage                     | derivation key (reuse / supersede signal)                                                                             | Output                              |
| ------------------------- | --------------------------------------------------------------------------------------------------------------------- | ----------------------------------- |
| `chunking`                | markdown hash + `splitter_version`                                                                                    | chunk identities + manifest + input |
| `document_summary`        | markdown hash + `summary_version` + prompt hash + model profile                                                       | one document summary                |
| `chunk_contextualization` | summary identity + chunk content keys + `splitter_version` + window + prompt hash + model profile + `context_version` | one variant per chunk               |
| `mention_extraction`      | `extractor_version` + `materializer_version` + `input_policy` + upstream run's key                                    | mentions + co-occurrences           |

A version stamp (`splitter_version`, `summary_version`, `context_version`, `extractor_version`) bumps whenever a pass's output changes for unchanged inputs; because it's part of the derivation key, a bump supersedes even when the source content is identical.
Mention extraction's _upstream run's key_ is the contextualization run's when `input_policy = contextualized`, else the chunking run's — so a change anywhere upstream flows through.

## The artifact stores

### Chunks

Facts are split by what they are _about_, so a chunk keyed by a stable surrogate stays honest across every generation that re-emits it:

- **`graph_chunks`** — immutable chunk identities carrying stable facts only: `chunk_id` (the surrogate), `content_hash`, `source_id`, `heading_path`, `ordinal`, `text`, `char_count`.
  An unchanged chunk keeps one row and one surrogate across re-chunks; its **reuse key** is `(source_id, heading_path, ordinal, content_hash)`.
- **`graph_chunk_run_inputs`** — one row per run: what it _consumed_ (the `conversion_output_id` locator + the markdown hash that verifies it).
- **`graph_chunk_run_manifest`** — append-only `(run_id, chunk_id, span)`: what a run _produced_, and where each chunk sat in _that_ generation's markdown.

Generation-varying facts — the markdown hash, `splitter_version`, each chunk's `span` — live on the run/input/manifest, not on the shared identity.
`chunks_of_run` reconstructs a full round-trip `Chunk` by joining chunk ⋈ manifest ⋈ input ⋈ run.

### Contextualized variants

`summarize_document` writes one document summary; `contextualize_chunks` rewrites each chunk into a **self-contained revision** — or an empty string when the chunk already stands alone.
The variant is stored separately; the raw chunk row is never rewritten and stays the cited, source-faithful unit.

At consume time, `resolve_chunk_text(working_text, *, contextualized_text, contextualization_enabled)` picks raw vs. revised and records which it used (`ContextSource.RAW` / `CONTEXTUALIZED`), enabling a raw-vs-contextualized comparison downstream.
An empty revision means "already self-contained": the consumed text is the raw chunk unchanged.
The toggle is a consumption/eval lever — and an enqueue lever for bounding the first backfill — never a per-unit branch that emits chunks without variants.

### Mentions

Extraction reads a source's persisted chunks — and, when available, their active contextualized variant — and writes an append-only, purely lexical **mention store**: the dataset entity canonicalization is later calibrated against.

- **`graph_mentions`** — append-only, run-scoped mention rows; `mention_id` is a surrogate (a fresh UUID, same reasoning as `chunk_id`).
- **`graph_mention_cooccurrences`** — one row per unordered intra-chunk mention pair within a run (`(run_id, mention_id_lo, mention_id_hi)`, canonical `lo < hi`).
  Co-occurrence lives only here, never on the mention row.

Two things make a mention's provenance legible:

- **Where it was read from** — `input_kind` (`raw` / `contextualized`) plus `input_ref`, canonical JSON pinning exactly the chunk (and, for a variant, its `context_version` + contextualization run).
  A mention read from raw input is always `source`-anchored.
  If a variant's text is empty (chunk already self-contained), extraction records the mention against the _raw_ chunk, never a contextualized ref that would dereference to empty text.
- **Where it sits** — `anchor_kind` says whether the surface form was found in the raw chunk (`source`) or exists only because a revision resolved a reference inline (`revision`).
  A `source`-anchored form appears once **per raw occurrence** (three occurrences ⇒ three rows), each carrying its offsets into the raw chunk; a `revision`-anchored mention has chunk-granularity provenance instead.

Each source-anchored mention also carries a **`source_occurrence_key`** — a hash of `(chunk_id, source_chunk_span, source_anchor_text)`.
It is a _reuse key_, not an identity: two runs that both detect the same raw occurrence produce two distinct `mention_id` rows with equal `source_occurrence_key`s, which is what lets you align a gold set and diff run-to-run.

**No stored embedding.**
Mentions carry no vector.
The disambiguation-context embedding canonicalization needs is recomputed on demand from a mention's `(input_kind, input_ref, input_span)` at decision time (ADR-006) — keeping extraction purely lexical, so an encoder change strands nothing.
Candidate-generation ("blocking") keys are likewise derived downstream from the stored `surface_form`, not persisted here.

For the finer points — why `input_span` (offsets into the text actually read) and `source_chunk_span` (offsets into the raw chunk) are distinct coordinate systems, and why same-surface rows share the first detection's `input_span` — see the `SpanCoordinateSystem` decision in [`design.md`](../../../.specs/changes/mention-extraction-foundation/design.md).

## Extractors

NER sits behind a pluggable `EntityExtractor` interface (`aizk.graph.extraction`): two pinned production implementations plus a deterministic stub for tests.

| Extractor          | Model                                               | Install                        | Weights                                                                        |
| ------------------ | --------------------------------------------------- | ------------------------------ | ------------------------------------------------------------------------------ |
| `SpacyExtractor`   | spaCy `en_core_web_sm` 3.8.0                        | opt-in `ner` group (URL-wheel) | bundled in the wheel                                                           |
| `Gliner2Extractor` | GLiNER2 `gliner2==1.3.2`, `fastino/gliner2-base-v1` | opt-in `ner` group             | pre-fetched by `aizk-graph fetch-gliner2-weights`; never downloaded at runtime |
| stub (tests)       | deterministic, fixed detections                     | none                           | n/a                                                                            |

`extractor_version` encodes the extractor, model, and configuration (e.g. GLiNER2's zero-shot entity-label schema), so any swap is an observable, versioned run input rather than an in-place change.
Both real extractors are lazily imported — the contract suite runs on the stub without the `ner` group.
Determinism (same input → identical detections at pinned versions) is verified opt-in: `AIZK_RUN_NER_DETERMINISM=1 uv run pytest tests/graph/test_extractor_determinism.py`.
The techstack choice is an addendum to [ADR-006 §3](../../../docs/decision-record/006-graph-construction-entity-canonicalization.md).

## Using it

### What you consume

Domain functions take a session and never commit — the caller owns the transaction:

- Chunks: `persist_chunks`, `chunks_of_run`, `current_chunk_ids`, `active_chunking_run`, `manifest_of_run`, `run_input`, `reconstruct_chunk`.
- Contextualization: `summarize_document`, `contextualize_chunks`, `resolve_chunk_text`.
- ORM rows: `Chunk`, `ChunkRunInput`, `ChunkRunManifest`, `DocumentSummary`, `ContextualizedChunk`, plus the mention tables.
- LLM seam: `LLMClient` (one `generate(prompt)` access point), `PydanticAILLMClient`, `StubLLMClient`.

Orchestration:

- `process_document(session, client, job, markdown_source) -> ProcessResult` — the single write path.
- `enqueue_document` / `enqueue_backfill` (domain) and `enqueue_output` / `enqueue_backfill_outputs` (resolve `conversion_output_id → ConversionOutput.source_id`).
  Both modes dedupe on `idempotency_key`, honor the stage's declared capacity, and feed the one write path, so the produced records are run-mode-independent.
- Derivations: `pending_contextualization` (with its `_outputs` / `_sources` projections), `pending_extraction_sources`, `stale_extraction_sources`.
  Each is read-only, so the same query feeds both admission and the operator's coverage view and the two cannot disagree.
- Admission: `AdmissionAdapter`, `admission_adapter_for`, `run_admission_pass`, `AdmissionLoop`; `StageAtCapacityError` and the `capacity` helpers.
- `ContextualizationStageHandler` — the runtime `StageHandler` (claim / execute-in-own-transaction / finalize / recover / cancel; `ValueError` → permanent, other exceptions → retryable, success → succeeded; in-process, single-writer).
- `MarkdownSource` / `S3MarkdownSource` (over a `BlobReader`), `ContextualizationConfig`, `build_llm_client`, `run_graph_worker`.

Operator surface (`aizk.graph.api`): the JSON API — `POST /v1/contextualizations` and `POST /v1/extractions` to submit work, plus `GET` (+ status filter), `GET /{id}`, `POST /{id}/retry`, `POST /{id}/cancel` per stage — and the operator console (`aizk.console`): a descriptor-driven dashboard (`/ui`), a cross-stage task monitor (`/ui/tasks?stage={key}`) with per-unit drill-down, and the content explorer (`/ui/explore/chunks`).

A stage that declares a pending-work derivation also gets a **pending** count on the dashboard and a listing of the sources behind it on its monitor page — work that by definition has no work-unit, so it cannot appear in the unit table.
One that declares a staleness derivation gets a **stale** count and stale-marked monitor rows, selectable for its declared actions.
Both are feature-detected, so a stage without the concept shows no figure.
Extraction declares `Re-extract`: a requeue of a finished unit whose source is stale, which the worker then re-runs against the source's current active inputs, superseding the prior run.
It is the only path to re-extraction and is always operator-initiated.

### CLI

`aizk-graph` console script (or `python -m aizk.graph.cli`):

- `aizk-graph worker` — the per-document write path (split → summarize → contextualize → persist).
- `aizk-graph serve` — the operator API + UI over uvicorn, on its own listener (default `0.0.0.0:8001`) so it runs alongside the conversion API.
- `aizk-graph extraction-worker` — the per-source extraction write path (mirrors `worker`).
  The extractor and `input_policy` are worker configuration, not work-unit fields: swapping either opens a new derivation-keyed run for the same source, never a new job kind.
- `aizk-graph fetch-gliner2-weights` — the one-time pre-fetch of GLiNER2's pinned weights into the configured local directory; never part of worker startup, since a stage adapter must not reach the network for model weights.
- `aizk-graph extract-dataset` — a **synchronous, foreground** pass over an explicit target set (every source with an active chunking run, optionally capped by `--limit N`, or a repeatable `--source-id`).
  A corpus-scanning selection (no `--source-id`) is gated behind explicit confirmation (`--yes`), mirroring the backfill enqueue's confirmation gate; named `--source-id`s run unconfirmed.
  Re-running over an unchanged corpus is a cheap no-op.
  It prints the corpus mention dataset's cold-start statistics as JSON, always over the **full** corpus (every source's active run): singleton rate, mentions per chunk, and co-occurrence density, each partitioned by anchor class (definitions in [`design.md`](../../../.specs/changes/mention-extraction-foundation/design.md)).

No `aizk-graph` command manages Litestream: the graph stage reuses the conversion database, whose replication is owned by the conversion service.
Migrations run on worker startup or via `aizk-conversion db-init` over the shared Alembic tree; `serve` does not migrate.

### Configuration

The graph stage resolves the shared database URL from `DatabaseConfig` (the stage-independent `aizk.db` foundation) and reuses the conversion service's `ConversionConfig` for shared S3/application settings.
Its own settings ([`config.py`](config.py)):

- `AIZK_GRAPH__CONTEXTUALIZATION__*` — the OpenAI-compatible model endpoint triple (`LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`), the worker's lease/retry knobs, and the operator listener (`OPERATOR_API_HOST` / `OPERATOR_API_PORT` / `OPERATOR_API_RELOAD`).
- `AIZK_GRAPH__EXTRACTION__*` — the extractor choice and `input_policy` for `extraction-worker`.
- `AIZK_GRAPH__NER__*` — the local GLiNER2 weights directory.
- `AIZK_GRAPH__*` (admission and capacity, spanning both stages) — `ADMISSION_CONTEXTUALIZATION_ENABLED` / `ADMISSION_EXTRACTION_ENABLED` (both off by default) and `ADMISSION_INTERVAL_SECONDS`; `CONTEXTUALIZATION_QUEUE_MAX_DEPTH` / `EXTRACTION_QUEUE_MAX_DEPTH` (`0` = no limit) and `QUEUE_RETRY_AFTER_SECONDS`.

The write-path worker refuses to start when the model endpoint is not fully configured; `serve` needs only the shared database.

## References

- Delta specs: `.specs/changes/chunk-persistence-contextualization/specs/{chunking,chunk-contextualization}/spec.md` (synced into the baseline after the change lands); `.specs/changes/mention-extraction-foundation/specs/{mention-store,entity-extraction,schema-migrations}/spec.md`.
- Design decisions: [`design.md`](../../../.specs/changes/archive/2026-06-03-chunk-persistence-contextualization/design.md) (chunking/contextualization) and [`design.md`](../../../.specs/changes/mention-extraction-foundation/design.md) (mention extraction); ADR-003 (database), ADR-004 (model provider), ADR-005 (pipeline pattern), ADR-006 (repairability; §3 carries the pinned-extractor techstack addendum).
- Shared runtime: [`aizk.pipeline`](../pipeline/README.md).
