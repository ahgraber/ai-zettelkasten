# Design: Mention Extraction Foundation

## Context

This change extracts entity mentions from the chunks and contextualized variants the prior change persists, and writes them to an append-only lexical mention store — the dataset entity canonicalization is later calibrated against.

Constraints shaping the design:

- **Depends on `chunk-persistence-contextualization`.**
  Persisted chunks and contextualized variants are the input; `mention.chunk_id` is a foreign key into the chunk table that change creates, and extraction reuses that change's run/dataset-version model.
- **Prerequisite refactor (`pipeline-stage-runtime`).**
  Extraction runs as a worker-driven stage with an operator UI on the shared runtime, implemented before this change (build order: `pipeline-stage-runtime` → `chunk-persistence-contextualization` → this change).
  This change supplies the extraction stage adapter and the mention stores.
- **Database (ADR-003).**
  SQLite + SQLModel/Alembic, WAL, a single serialized writer, Litestream.
  Extraction inverts the write profile: ~10 mentions plus co-occurrence links per chunk is roughly two orders of magnitude more rows than chunking.
- **No stored vectors (ADR-006).**
  Mentions are lexical only; the context embedding is recomputed on demand at canonicalization time and is never persisted here, so no vector table is added.
- **Extraction quality is a measurement target, not an architectural guarantee (ADR-006).**
  Low span-level NER F1 distorts downstream graph metrics; the dataset must let that be measured against a gold set, and the NER model must be swappable.

Capabilities build in dependency order: `mention-store` (the data contract) before `entity-extraction` (which produces records into it).

## Decisions

### Decision: ReificationRunModel

**Chosen:** Mentions are produced by **reification runs**, reusing the unified stage-run/dataset-version model from `chunk-persistence-contextualization`. A `mention_run` records `run_id`, `extractor_version`, `reifier_version`, `input_policy`, the contextualization `input_fingerprint`, `supersedes_run_id`, and `status` (active | superseded). A `mention` is run-scoped: `mention_id = hash(run_id, chunk_id, source_chunk_span, surface_form)`. Each mention also carries `source_occurrence_key = hash(chunk_id, source_chunk_span, source_anchor_text)` as a non-primary, cross-run-stable diagnostic key. Invalidation is run-level: a changed extractor, reifier, input policy, or contextualization input opens a new run that supersedes the prior; mention rows are immutable. The invariant is **"one active mention dataset at a time."** The run's `scope_key` is corpus-wide (one active mention dataset for the corpus).

The run records two distinct versions: **`extractor_version`** versions the NER extractor (model + config — spaCy or GLiNER2), and **`reifier_version`** versions the deterministic post-NER reification logic that turns NER output into persisted mention records — span mapping (input→raw-chunk anchor), blocking-key derivation, co-occurrence linking, and `mention_id`/`source_occurrence_key` derivation.
A change to that logic bumps `reifier_version` without changing the NER model, and vice versa; `input_policy` records the raw-vs-contextualized input toggle the run was produced under.

**Rationale:** A persisted mention is the output of a specific reification run, not a timeless occurrence observed many ways — so modeling it as a run product (rather than an observation of an occurrence) keeps every downstream consumer reading the active run and ignoring superseded ones, without reasoning about per-observation history.
The `source_occurrence_key` preserves just enough cross-run continuity for gold-set alignment and run-to-run diffing without making it the row's identity.
Run-level invalidation matches the conversion stage's mutable-status + append-only-events pattern and the chunk/contextualization runs upstream.

**Alternatives considered:**

- **Occurrence-vs-observation split (stable occurrence id + per-observation rows).**
  Over-models: forces every consumer to reason about observations versus occurrences.
  The run model is simpler and matches the invalidation story.
- **Timeless `mention_id = hash(chunk_id, span, surface_form)` (no run).**
  A new extractor finding the same span collides with the old immutable row — unrepresentable under append-only.
  Rejected (this was the original defect).

### Decision: SpanCoordinateSystem

**Chosen:** Every mention records two spans with declared coordinate systems: `input_span` indexes the text extraction actually read (raw chunk or contextualized variant, identified by `input_ref`/`input_kind`), and `source_chunk_span` anchors the mention into the **raw chunk text**.
Extraction maps the NER offset back to a raw-chunk anchor.
For a mention arising from a resolved reference (the variant rewrote "it" → "the monarch butterfly"), `source_chunk_span` anchors to the referring expression ("it") in the raw chunk while `surface_form` is the resolved form.
`source_anchor_text` (used in `source_occurrence_key`) is the raw text at `source_chunk_span`.

**Rationale:** The raw chunk is the only stable coordinate system: it is what downstream provenance, `(chunk_id, span)` embedding recomputation, and the gold set anchor against.
Storing only the variant-text offset would break replay and embedding windows when contextualization is re-run or toggled.
Recording both spans keeps the lexical evidence (resolved `surface_form`) while preserving a stable raw-text anchor; anchoring resolved references to the referring expression gives every mention a raw-chunk home.

**Alternatives considered:**

- **Single span into whatever text was read.**
  Ambiguous coordinate system; breaks `(chunk_id, span)` recompute and gold alignment when the input text changes.
- **Drop resolved-reference mentions (no raw verbatim).**
  Loses exactly the mentions contextualization was added to recover.

### Decision: MentionSchemaWithChunkForeignKeyAndFlatCooccurrence

**Chosen:** A `mention` table (PK `mention_id`; `run_id`; `chunk_id` FK into the chunk table; `surface_form`, `source_chunk_span`, `input_span`, `input_kind`, `input_ref`, `blocking_keys`, `source_occurrence_key`; no embedding column).
Co-occurrence is a separate flat `mention_cooccurrence` link table — one row per unordered intra-chunk pair within a run (`(run_id, mention_id_lo, mention_id_hi, chunk_id)`).
Co-occurrence is **resolvable through that table**, never stored on the mention row.
No vector table.

**Rationale:** A real foreign key (chunks are persisted) makes "a mention's source chunk is resolvable" a database invariant.
A flat link table keeps the 1-hop neighbor lookups canonicalization needs cheap and indexable, maps cleanly onto the Postgres path, and removes the proposal/spec-vs-design inconsistency of also storing co-occurring IDs on the row.
The unordered pair stored once avoids double-counting while serving symmetric lookups.

**Alternatives considered:**

- **Co-occurring IDs as a column on the mention row.**
  Hostile to indexed pair queries and the eventual entity co-occurrence graph; and it duplicates state the link table already holds.
- **Two directed rows per pair.**
  Doubles row count (already the write-volume risk) and invites asymmetry bugs.

### Decision: TwoPinnedExtractorsSpacyAndGliner2

**Chosen:** NER sits behind a pluggable extractor interface owned by the graph stage, with **two concrete extractors available: spaCy and GLiNER2**.
Both model artifacts are **pinned as dependencies** (no runtime model downloads), and `extractor_version` encodes which extractor + model + configuration produced a run.
The dataset can be produced with either and compared; spaCy gives a fast deterministic general-domain baseline, GLiNER2 gives schema-free zero-shot extraction better aligned with emergent concepts.

**Rationale:** Building extraction first exists to produce a _measurable_ dataset; having both extractors pinned lets the gold-set evaluation decide which clears the F1 floor on our corpus rather than guessing a priori, and an extractor swap is an observable run input (new run), never an in-place change.
Pinning the model artifacts honors the pinned-dependency / reproducibility rule (no ad-hoc downloads).
The concrete spaCy model package and the GLiNER2 package/version + availability are confirmed at implementation via the research path.

**Alternatives considered:**

- **One hard-coded model.**
  Forecloses the comparison the dataset exists to enable.
- **spaCy default, GLiNER deferred.**
  The user wants both available now; pinning both keeps the comparison first-class. (Techstack recorded against the graph-stage decision record per the project's ADR requirement.)

### Decision: DeterministicStubExtractorForTests

**Chosen:** All `entity-extraction` and `mention-store` contracts are tested with a deterministic stub extractor returning known spans for known inputs.
The real spaCy / GLiNER2 models are exercised only by the offline F1 evaluation against the gold set, not by the contract suite.

**Rationale:** The spec contracts — span coordinates, co-occurrence symmetry, run-scoped identity, provenance, input selection, run-mode independence — are pipeline properties independent of which entities a real model finds.
A stub makes them deterministic, fast, and portable.
NER _quality_ is a separate, model-dependent measurement with its own evidence (the gold-set eval), so no verification waiver is needed for the contract requirements.

**Alternatives considered:**

- **Run the real models in contract tests.**
  Slow, non-portable, and conflates pipeline correctness with model quality.

### Decision: ExtractionRunsAsAStageWithBatchedWrites

**Chosen:** Extraction is a per-chunk (grouped per-document) unit of work registered as a stage on the shared runtime, invoked by both bulk/backfill and incremental entry points through one write path.
Mention and co-occurrence inserts are batched into a few transactions per document; corpus-wide backfill runs as throttled background work.

**Rationale:** One write path makes the run-mode-independence contract hold by construction.
Batching keeps the elevated write volume (two orders of magnitude over chunking) inside ADR-003's serialized-writer budget; WAL keeps retrieval readers unblocked.

**Source identity for runtime events.**
Extraction resolves the `aizk_uuid` source identity for each mention/run via the chunk → its converted artifact → `ConversionOutput.aizk_uuid`, and carries it onto the reification run and its transition events so a source's progress is resolvable across stages (the runtime's cross-stage event requirement).
The runtime ships no generic UI, so this stage builds its own operator view.

**Generic lifecycle mapping.**
An extraction work-unit maps onto the runtime's generic lifecycle: `succeeded` on completion; `failed` classified `retryable` on transient model/IO errors and `permanent` on unprocessable input; `cancelled` and `timed_out` per the harness.
Extraction runs in-process (no subprocess isolation), so the subprocess-specific termination guarantees do not apply.

**Reading the active contextualized variant.**
When selecting input, extraction reads a chunk's variant only from its document's **active** contextualization run (never a superseded one); `input_ref` is that variant's run-scoped address `(chunk_id, context_version, run_id)`.
This keeps the input-selection contract tied to current context rather than any historical variant.

**Alternatives considered:**

- **Row-at-a-time inserts.**
  Hammers the serialized writer at backfill scale.
- **Separate bulk/incremental write paths.**
  Invites the divergence the run-mode contract forbids.

## Architecture

```text
persisted chunk ──┬── contextualized variant available? ──► read variant   (input_kind=contextualized)
                  └── else ───────────────────────────────► read raw text  (input_kind=raw)
                                   │
                                   ▼
       pluggable NER extractor (spaCy | GLiNER2, both pinned; stub in tests)
                                   │
            map NER offset → input_span (read text) + source_chunk_span (raw-chunk anchor)
                                   │
                    ┌──────────────┴───────────────┐
                    ▼                               ▼
              mention records                 mention_cooccurrence
   (mention_id = hash(run_id, chunk_id,      (flat: run_id, lo, hi, chunk_id;
    source_chunk_span, surface_form);         one row per unordered intra-chunk pair)
    source_occurrence_key; chunk_id FK;
    input_kind/ref; NO embedding)
                    │
                    ▼
   mention_run (extractor/reifier versions, input_policy, input_fingerprint,
                supersedes_run_id, status active|superseded)  ── one active dataset
                    │
                    ▼
   (later) canonicalization reads the active run; recomputes embeddings on demand

runs as a stage on the shared pipeline-stage runtime; bulk batches, incremental per-doc.
```

## Risks

- **NER F1 below the distortion floor.**
  A weak extractor seeds spurious mentions and co-occurrence.
  Mitigation: the dataset measures span-level F1 against a gold set; two pinned extractors are comparable; a swap is a new run (versioned); mentions are recorded faithfully with provenance so artifacts are traceable.
- **Elevated write volume on backfill.**
  Mitigation: batch per document, throttle background backfill, keep reads on WAL.
- **Coupling to contextualization availability.**
  Mitigation: raw-text fallback with `input_kind`/`input_ref` recorded, so extraction proceeds and the raw-vs-contextualized comparison stays measurable.
- **Span mapping for resolved references.**
  Mapping a variant offset back to a raw-chunk anchor is non-trivial when contextualization rewrote text.
  Mitigation: anchor to the referring expression and keep the resolved form as `surface_form`; the stub-based tests pin the contract, and mismatched/unmappable spans are surfaced rather than silently dropped.
- **Superseded-run accumulation.**
  Mitigation: run-level invalidation is a cheap status transition; superseded-run content is reclaimed by the `artifact-compaction-retention` change.
