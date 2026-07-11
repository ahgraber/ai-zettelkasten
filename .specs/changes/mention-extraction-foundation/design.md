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

**Chosen:** Mentions are produced by **reification runs** on the shared stage-run/dataset-version primitive (`pipeline_runs`), **one run per source** — `scope_id = str(source_id)`, exactly like the chunking and contextualization runs upstream.
No stage-specific run table exists: the primitive's partial unique index enforces at most one active run per source, `record_run`/`reuse_or_record_run` give atomic per-source supersession, and the run's `version_stamps` record `extractor_version`, `reifier_version`, and `input_policy`.
The run's `derivation_key` is a deterministic hash over semantic inputs only — `(extractor_version, reifier_version, input_policy, the consumed upstream run's derivation_key)`: the source's active contextualization run's key when `input_policy = contextualized`, else its active chunking run's key.
Local surrogate ids (`run_id`s, row ids) never enter the key.

A `mention` is run-scoped: its `mention_id` is a surrogate assigned at persistence (never content-derived), and per-anchor-class within-run uniqueness (source-anchored: `(run_id, chunk_id, source_chunk_span, surface_form)`; revision-anchored: `(run_id, chunk_id, surface_form)`) makes re-persisting the same detection idempotent — the contextualized-variant pattern (`UNIQUE(run_id, chunk_id)`) one level finer.
Each source-anchored mention also carries `source_occurrence_key = hash(chunk_id, source_chunk_span, source_anchor_text)` as a non-primary, cross-run-stable diagnostic key; revision-anchored mentions diff across runs by `(chunk_id, surface_form)`.
Invalidation is run-level and lazy, per source: a changed extractor, reifier, input policy, or consumed upstream input changes that source's derivation key, so the source's next reification opens a superseding run; other sources' active runs are untouched, and mention rows are immutable.
The invariant is **one active reification run per source**; the corpus mention dataset is the union of the sources' active runs — the same way the active contextualized corpus is already the union of per-source contextualization runs.
A consumer needing a frozen snapshot records the set of run ids it read (a read-time manifest); no corpus-level lifecycle object exists.
Per-mention provenance is finer-grained still: each mention's `input_ref = (chunk_id, context_version, run_id)` pins the exact upstream run it was read from, so the consumed input is resolvable per row, not only in aggregate.

The run records two distinct versions: **`extractor_version`** versions the NER extractor (model + config — spaCy or GLiNER2), and **`reifier_version`** versions the deterministic post-NER reification logic that turns NER output into persisted mention records — span mapping (input→raw-chunk anchor), blocking-key derivation, co-occurrence linking, and `mention_id`/`source_occurrence_key` derivation.
A change to that logic bumps `reifier_version` without changing the NER model, and vice versa; `input_policy` records the raw-vs-contextualized input toggle the run was produced under.

**Rationale:** A persisted mention is the output of a specific reification run, not a timeless occurrence observed many ways — so modeling it as a run product (rather than an observation of an occurrence) keeps every downstream consumer reading active runs and ignoring superseded ones, without reasoning about per-observation history.
Source-scoping reuses the primitive unchanged and keeps activation atomic per source: a run and its mentions become visible together, so an active run is never a partially-built dataset.
It also makes invalidation incremental — ingesting or re-contextualizing one document supersedes only that document's mention run, which is what the incremental entry points require.
The `source_occurrence_key` preserves just enough cross-run continuity for gold-set alignment and run-to-run diffing without making it the row's identity.
Run-level invalidation matches the conversion stage's mutable-status + append-only-events pattern and the chunk/contextualization runs upstream.

**Alternatives considered:**

- **A corpus-wide run with a `consumed_input_fingerprint` over all active upstream runs.**
  Under the two-status primitive a corpus run is born active and stays incomplete for an entire backfill while the prior complete dataset is already superseded; and any single-source change (including a newly ingested document) changes the fingerprint and re-keys every mention in the corpus — an O(corpus) rewrite of immutable rows per document change, incoherent with incremental mode.
- **A candidate/building run lifecycle (membership snapshot + atomic activation + revalidation) to rescue corpus scope.**
  Adds a third status to the shared primitive plus snapshot and activation-time revalidation machinery; revalidation can starve on an actively-ingesting corpus.
  Source-scoping makes all of it unnecessary.
- **A stage-specific `mention_run` table.**
  Re-declares status, supersession, and the one-active-run enforcement the shared primitive already provides.
- **Content-derived `mention_id`** — `hash(run_id, …)` embeds a local surrogate in a durable identity (non-portable across a logical migration); `hash(derivation_key, …)` collides across runs when a superseded key is later re-derived (an input toggled back), leaving the new run without rows of its own.
  The surrogate id plus the within-run uniqueness constraint gives the same idempotency with neither defect.
- **Occurrence-vs-observation split (stable occurrence id + per-observation rows).**
  Over-models: forces every consumer to reason about observations versus occurrences.
  The run model is simpler and matches the invalidation story.
- **Timeless `mention_id = hash(chunk_id, span, surface_form)` (no run).**
  A new extractor finding the same span collides with the old immutable row — unrepresentable under append-only.
  Rejected (this was the original defect).

### Decision: SpanCoordinateSystem

**Chosen:** Every mention records `input_span` — character offsets into the text extraction actually read (raw chunk or contextualized variant, identified by `input_ref`/`input_kind`) — and an **`anchor_kind`** of `source` or `revision`.
Classification enumerates rather than assigns: for each detected surface form, the reifier searches the raw chunk text for occurrences of that surface form.
One or more occurrences ⇒ one **source-anchored** mention per occurrence, each carrying that occurrence's `source_chunk_span`; zero occurrences (the name exists only because the revision resolved a reference inline) ⇒ one **revision-anchored** mention with no `source_chunk_span`, whose raw provenance is chunk-granularity: `chunk_id` plus the exact variant read (`input_ref`, `input_span`).
Detections are never mapped to occurrences positionally or fuzzily; the classification is deterministic in the raw chunk text and the surface form alone, and any change to the rule is a `reifier_version` bump.
The two spans serve two consumer families: `input_span` is the **working span** — the disambiguation-context embedding is recomputed over the input text with the mention marked at `input_span` — while `source_chunk_span` serves provenance, cross-run occurrence diffing, and the span-level gold set.
`source_anchor_text` (used in `source_occurrence_key`) is the raw text at `source_chunk_span`; both exist only on source-anchored mentions.
When several detections of one surface expand to per-occurrence rows, each row carries the first detection's `input_span` — so same-surface rows within one chunk share one marked disambiguation context and will canonicalize identically.
This is accepted, not incidental: distinguishing same-surface, different-referent occurrences inside a single chunk is beyond marked-window embedding resolution under any representation (the windows barely differ), the coherent-discourse case makes the shared assignment correct, and the homonym residue is a recorded risk measured by the span-level gold set.
Per-detection counts remain visible in run statistics.

**Rationale:** Provenance, replay, and the gold set need a **stable** frame: raw chunk text is content-addressed and survives contextualization re-runs, so span-level ground truth anchors there.
Disambiguation and downstream semantic processing need the **richer** frame: the revision names referents the raw text only alludes to, so the working span points into what was actually read.
A revision-anchored mention is still a verifiable claim: its `input_ref` resolves to a persisted, immutable variant whose own provenance reaches the summary and raw chunk, and the reification run's derivation key embeds the contextualization run's key, so an active mention's variant is always the active variant — never dangling.
The anchor class is recorded because it is free to compute (the occurrence search runs regardless) and is the bit every downstream trust policy needs; without it, how much to rely on revision-born mentions is unanswerable and unmeasurable.
Per-occurrence expansion keeps every persisted span exactly true ("this string occurs here"), preserves within-chunk frequency for the terms repetition marks as salient, and eliminates the detection-to-occurrence assignment problem entirely.
Class-partitioned dataset statistics, a span-level gold frame for source-anchored mentions, and a chunk-level association gold frame for revision-anchored mentions make each class's quality a measurement, not an assumption.

**Alternatives considered:**

- **Persist only mentions with a unique verbatim anchor; discard the rest.**
  Silently drops the entities contextualization exists to surface and every repeated surface form — biasing the dataset against pronoun-referenced and high-salience terms with nothing to measure the loss by.
- **Pin ambiguous repeats to the first raw occurrence.**
  Deterministic but asserts a specific span that may be the semantically wrong occurrence, and erases within-chunk frequency.
- **Positional or fuzzy mapping of rewrite offsets onto raw occurrences.**
  A reordered or expanded revision silently shifts every subsequent pin — wrong-but-confident provenance, the worst failure class for a provenance-first system.
- **`source_chunk_span` as a list of spans on one row.**
  The span is load-bearing relationally (per-class identity, `source_occurrence_key`, gold-set span joins); a JSON list forces every consumer through unpacking and forecloses referencing a single occurrence, which a future referent-resolution artifact needs.
- **Anchor revision-born names to their raw referring expressions ("the company").**
  Requires a structured resolution map the revision does not store; guessing the anchor violates provenance.
  A future referent-resolution artifact can promote revision-anchored mentions to referent anchors.
- **Single span into whatever text was read (chunk-level raw provenance for all).**
  Degrades the source-anchored majority's exact anchors to accommodate the minority; breaks span-level gold scoring, occurrence diffing, and the `(chunk_id, span)` audit trail.

### Decision: MentionSchemaWithChunkForeignKeyAndFlatCooccurrence

**Chosen:** A `mention` table (surrogate PK `mention_id`; `run_id`; `chunk_id` FK into the chunk table; `anchor_kind` `source`|`revision` with a CHECK constraint; `source_chunk_span` and `source_occurrence_key` nullable and populated iff source-anchored, CHECK-enforced; per-class partial unique indexes — source: `(run_id, chunk_id, source_chunk_span, surface_form)`, revision: `(run_id, chunk_id, surface_form)` (SQL UNIQUE treats NULLs as distinct, so a single index over the nullable span would not deduplicate revision rows); `surface_form`, `input_span`, `input_kind`, `input_ref`, `blocking_keys`; no embedding column).
Raw input implies source anchoring (`input_kind = raw` ⇒ `anchor_kind = source`), validated at the persistence boundary.
Co-occurrence is a separate flat `mention_cooccurrence` link table — one row per unordered intra-chunk pair within a run (`(run_id, mention_id_lo, mention_id_hi, chunk_id)`).
The pair key is schema-enforced: composite primary key `(run_id, mention_id_lo, mention_id_hi)`, `CHECK(mention_id_lo < mention_id_hi)` (canonical order, which also excludes self-pairs), and both endpoints foreign keys to `mention.mention_id`; persistence validates that the endpoints belong to the same run and chunk, and link inserts are idempotent on the pair key — so a retried chunk cannot duplicate or disorder links.
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
A document's reification commits atomically: the run record and all of its mention and co-occurrence rows are staged and committed in **one transaction**, so a partial failure exposes no active run for the source and no readable mentions from the failed attempt; a retry re-enters through the unchanged derivation key (`reuse_or_record_run`) and idempotent inserts.
Corpus-wide backfill runs as throttled background work.

**Rationale:** One write path makes the run-mode-independence contract hold by construction.
Batching keeps the elevated write volume (two orders of magnitude over chunking) inside ADR-003's serialized-writer budget; WAL keeps retrieval readers unblocked.

**Source identity for runtime events.**
Extraction resolves the durable source identity for each work unit directly from the chunk's `source_id` — the persisted chunk carries it as a stable fact, so no hop through a per-conversion artifact is needed.
The reification run is itself source-scoped (`scope_id = str(source_id)`), and the work unit's transition events carry `source_id`, so a source's progress is resolvable across stages (the runtime's cross-stage event requirement).
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
            record input_span (read text); search raw chunk for each detected surface:
            ≥1 occurrences → one source-anchored mention per occurrence (source_chunk_span)
            0 occurrences  → one revision-anchored mention (no source_chunk_span)
                                   │
                    ┌──────────────┴───────────────┐
                    ▼                               ▼
              mention records                 mention_cooccurrence
   (surrogate mention_id; anchor_kind        (flat: run_id, lo, hi, chunk_id;
    source|revision; per-class partial        composite PK + CHECK(lo < hi);
    unique indexes; source_occurrence_key     one row per unordered intra-chunk pair)
    on source rows; chunk_id FK;
    input_kind/ref; NO embedding)
                    │
                    ▼
   per-source reification run on pipeline_runs (scope_id = str(source_id);
   derivation_key = hash(extractor/reifier versions, input_policy,
   consumed upstream run's derivation_key); versions in version_stamps;
   status active|superseded)  ── one active run per source; corpus dataset = union
                    │
                    ▼
   (later) canonicalization reads the active runs; recomputes embeddings on demand

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
- **Revision-anchored mention quality.**
  The revision can resolve a reference to the wrong referent, and a revision-anchored mention has no raw-span witness.
  Mitigation: `anchor_kind` is on every row so consumers can weight or exclude the class; dataset statistics are partitioned by anchor class; span-level gold covers source-anchored rows while a chunk-level association gold frame covers revision-anchored ones; a stored resolution map (future change) can promote revision-anchored mentions to referent anchors.
- **Per-occurrence expansion over-emitting on same-surface homonyms.**
  Expansion asserts every raw occurrence of a detected surface is a mention; two same-cased homonyms of one surface within a single chunk would both be emitted.
  Mitigation: rare within one chunk's discourse, and measurable against the span-level gold set.
- **Version-mixed corpus view during an extractor/reifier rollout.**
  Per-source runs supersede lazily, so mid-backfill the union view mixes sources reified under old and new versions.
  Mitigation: the state is observable (every run records its versions), transient (drain the backfill), and filterable (consumers can restrict to a version or record the run-id set they read); this mirrors how chunk and variant generations already behave after a version bump.
- **Superseded-run accumulation.**
  Mitigation: run-level invalidation is a cheap status transition; superseded-run content is reclaimed by the `artifact-compaction-retention` change.
