# Proposal: Mention Extraction Foundation

## Intent

With contextualized chunks persisted by the prior change, the graph stage can extract entity mentions and persist them as an append-only, lexical ground-truth dataset.
That dataset is the prerequisite for designing entity canonicalization on evidence rather than transferable defaults: the create-vs-assign thresholds, blocking strategy, and extraction-quality floor can only be calibrated against real mentions from this corpus.

This change builds the extraction step and the store it writes to:

- Run NER over each chunk's selected input (contextualized when available, raw otherwise) to emit entity mentions with their surface form and a deterministic anchor: an exact raw-chunk span per occurrence when the name appears in the source text, or explicit revision-derived, chunk-granularity provenance when only the revision surfaced it.
- Link mentions that share a chunk via co-occurrence — the raw substrate later edge and canonicalization work reads.
- Persist mentions to an append-only store, never mutated, carrying the lexical evidence (aliases, blocking keys) and provenance (source chunk, anchor class and spans, extractor version) needed for replayable canonicalization.

The deliverable is a **runnable extraction pipeline and the populated mention store it yields over the corpus** — the ready-to-go dataset the canonicalization change consumes.
No context embeddings are produced or stored: the canonicalization resolver recomputes a mention embedding on demand from the mention's recorded input text and `input_span` at decision time, so extraction stays lexical and an encoder choice strands nothing here.

## User Stories

### Story: grounded-entity-graph

As the graph's owner, I want every entity mention anchored to a real chunk — with the exact source-text span for each occurrence when the name appears in the source text, and an explicit revision-derived marking when only the contextualized revision surfaced it — so that the knowledge graph is built on verifiable, trust-classified evidence from across my corpus rather than ungrounded guesses.

### Story: replayable-duplicate-free-dataset

As an operator, I want the mention dataset append-only, versioned, and idempotent with complete provenance, so that re-extraction and recovery never corrupt or duplicate the ground truth.

## Scope

This change depends on `chunk-persistence-contextualization`: it reads persisted contextualized chunks as its input.
Capabilities are listed in build-dependency order: `mention-store` (the data contract extraction targets) before `entity-extraction` (which produces mentions and writes them).

**In scope:**

- **`mention-store` capability (new).**

  - An append-only persisted store of mention records.
    Each mention carries: surface form, normalized aliases, redundant blocking keys (normalized / phonetic / acronym / token-shingle), source `chunk_id`, an `anchor_kind` (`source` | `revision`), an `input_span` (offsets into the text read), `input_kind`/`input_ref`, and — for source-anchored mentions — a `source_chunk_span` (offsets into the raw chunk) with a `source_occurrence_key`.
    A detected surface that occurs in the raw chunk yields one source-anchored mention per occurrence; a name that exists only because the revision resolved a reference inline is persisted as a revision-anchored mention whose raw provenance is the chunk plus the recorded input.
    Co-occurrence is resolvable through a link table, not stored on the mention row.
  - **No embedding field** — the context vector is recomputed on demand at canonicalization time, never persisted.
  - The append-only invariant (records are never mutated or deleted after write) and provenance-completeness invariant (every required provenance field populated).
  - Mentions are produced by **reification runs** (the unified run/dataset-version model): one run per source on the shared run primitive, recording the extractor/reifier versions and input policy with a derivation key over the consumed upstream inputs, and `status` (active|superseded); a mention is a run-scoped surrogate-id row unique per `(run_id, chunk_id, source_chunk_span, surface_form)`, with `source_occurrence_key = hash(chunk_id, source_chunk_span, source_anchor_text)` as a stable cross-run diagnostic key.
    Invalidation is run-level and per-source ("one active reification run per source"; the corpus dataset is the union of active runs); rows are immutable.

- **`entity-extraction` capability (new).**

  - NER over each selected chunk input emitting entity mentions (surface form + character span), classified deterministically by raw-occurrence search into source-anchored (one mention per raw occurrence) or revision-anchored (no raw span) records.
  - **Intra-chunk co-occurrence**: mentions sharing a chunk are linked in a flat co-occurrence link table — the substrate canonicalization will read; this change records the links, it does not materialize an entity-level graph.
  - Redundant blocking-key derivation per mention (the lexical candidate-generation index canonicalization will use).
  - An `extractor_version` captured on every mention.
  - Invocable in both **bulk/backfill** and **incremental** modes; the persisted output is identical regardless of mode.

**Out of scope (named, separate/later changes):**

- **Entity canonicalization** — the append-only-derived entity store, `entity_id` lineage handles, the lineage log, and the create-vs-assign resolver.
  The next change, designed against the dataset this one produces.
- **Mention context embeddings** — recomputed on demand at canonicalization resolve time using a marked-mention encoder; never produced or stored here.
- **Topology-based split detection** — ego-splitting, WSI, local-PPR, structural-role nominators/ratifiers; gated behind a validation gate.
- **Typed relation extraction** (e.g. GLiRel-style) — co-occurrence only here.
- **Materialized graph edges** (document-structure or entity co-occurrence graph) and **graph-aware retrieval.**

## Approach

> Mechanism sandbox; contracts live in the delta specs, chosen mechanisms formalize in `design.md`.

Extraction runs in the graph-stage package, reading persisted contextualized chunks when available and writing mentions:

- **NER** runs behind a pluggable extractor interface with two concrete extractors available — **spaCy** (fast, general-domain) and **GLiNER2** (schema-free, zero-shot) — both with model artifacts pinned as dependencies (no runtime download).
  The model choice and its extraction-F1 floor (low extraction F1 distorts every downstream graph metric) are a `design.md` decision and an explicit measurement target of the produced dataset.
- **Co-occurrence** is intra-chunk: every pair of mentions in the same chunk is linked.
  This is recorded on the mention records, not built into a separate entity graph (which presupposes canonical entities).
- **Blocking keys** (normalized / phonetic / acronym / token-shingle + MinHash via `rensa`) are derived at extraction time so the mention store doubles as the canonicalization candidate-generation index.
- **No embeddings.**
  Extraction produces purely lexical records.
- **Idempotency:** mentions are unique per `(run_id, chunk_id, source_chunk_span, surface_form)`; re-extraction of the same chunk within the same run is a no-op, and a changed extractor/input opens a new run for that source rather than colliding with prior immutable rows.
- **DB-ops profile.**
  Extraction inverts the pipeline's write profile: ~10 mentions plus their co-occurrence links per chunk is roughly two orders of magnitude more rows than chunking, written in bursts during ingest and backfill.
  Batch mention and co-occurrence inserts into a few transactions per chunk or per document against the serialized SQLite writer; run corpus-wide backfill as throttled background work, not foreground ingest.

Because NER is a noisy sensor, the testable contracts assert **structure, provenance, span coordinates, and run-scoped identity** — every emitted mention has a `chunk_id`, an `input_span`, and a deterministic anchor class (source-anchored with a raw-chunk `source_chunk_span` per occurrence, or revision-anchored with chunk-granularity raw provenance), and each mention belongs to a versioned run; co-occurrence is resolvable within a chunk; re-extraction within a run produces no duplicates — not exact mention sets.
Tests stub the extractor; the real spaCy / GLiNER2 models are exercised only by the offline gold-set evaluation.

## Decisions Carried Into Design

- **Mentions are the deliverable and the ground truth.**
  The output is an append-only lexical mention dataset; entities are not derived here.
  Mentions are never mutated — the precondition for the canonicalization change's repairability guarantee.
- **Lexical-only, no stored vectors.**
  No embedding is computed or persisted; the embedding signal is a canonicalization-time, on-demand recomputation.
  This keeps the largest potential storage line item (~one vector per mention) at zero by design.
- **Co-occurrence is recorded, not materialized.**
  Raw `mention ↔ mention` co-occurrence is persisted so the entity co-occurrence graph can be built later without re-extraction.
- **Blocking keys produced now** so the dataset is immediately usable as the create-vs-assign candidate index.
- **NER model is a design decision with a measurement obligation.**
  The dataset must let us measure span-level extraction quality before canonicalization relies on it.

## Schema Impact

**OpenAPI (`conversion-api`):** No changes.
This change introduces no HTTP endpoints; extraction is an internal pipeline stage with no API surface.
The `before/` snapshot equals the committed baseline `.specs/schemas/conversion-api-openapi.json`, and that snapshot is the expected post-change snapshot — no `schemas/expected.md` is generated.

**Database (SQLite):** Adds new tables and an Alembic migration — an append-only mention table and a co-occurrence link table.
No vector table is added (no mention embeddings are stored).
These are **not** tracked by `.specs/.sdd/schema-config.yaml`, so no DB snapshot is captured here; the `schema-migrations` capability's ORM-vs-migration equivalence test covers structural fidelity instead.
Concrete table shapes and migration-tree placement are decided in `design.md`.

## Open Questions

Most earlier questions are now settled in `design.md`: identity is run-scoped (per-source reification runs on the shared run primitive, surrogate mention ids unique within a run, with `source_occurrence_key` for cross-run continuity); `mention.chunk_id` is a foreign key into the persisted chunk table; co-occurrence is a flat link table (resolvable, not stored on the row); extraction reads the contextualized variant when available and records `input_kind`/`input_ref`, falling back to raw text otherwise; and both spaCy and GLiNER2 are pinned pluggable extractors.
Remaining:

- **GLiNER2 package/version availability.**
  Confirm the concrete GLiNER2 package, model artifact, and pinned version at implementation (via the research path), and the spaCy model package to pin.
  Both must be installable as pinned artifacts with no runtime download.
- **Extraction-quality eval ownership.**
  Span-level F1 against a gold set is the quality measurement (not a contract SHALL here).
  The gold set and eval harness are established with the canonicalization validation-gate work; this change produces the dataset and basic cold-start statistics that feed it.
- **NER model techstack ADR.**
  Per CLAUDE.md, the spaCy/GLiNER2 choice is recorded as an addendum to the graph-stage decision record (ADR-006 §3) when implemented.
