# Design: Identity & Provenance Foundation

## Context

Constraints shaping this design:

- **The run primitive already exists and already enforces half the idempotency story.**
  `pipeline-stage-runtime` provides `pipeline_runs` keyed by `(stage, scope_key)` with a partial unique index guaranteeing "at most one active run per scope" and atomic supersession (`record_run`).
  This change does **not** rebuild it — it renames `scope_key` → `scope_id` and references the invariant from `pipeline-identity`.
- **Database (ADR-003).**
  SQLite + SQLModel/Alembic, WAL, a single serialized writer, Litestream.
  Postgres is a planned peer backend (`pluggable-database`), so identities and provenance keys must be **portable** — no database-local surrogate may leak into a content-addressed id, a derivation key, or any cross-row reference.
- **The system is pre-release with no external API consumers.**
  This is what makes the breaking `aizk_uuid` → `source_id` rename across the public conversion API acceptable now rather than after a deprecation window.
- **Two producer kinds coexist.**
  Deterministic producers (the chunk splitter) and stochastic producers (the contextualization LLM, and the future mention extractor) need the _same_ identity/idempotency story expressed two ways — the split is load-bearing in several decisions below.
- **This is the prerequisite for `mention-extraction-foundation`**, which is revised to conform once this lands.

Capabilities build in dependency order: `pipeline-identity` (the contract) → `chunking` / `chunk-contextualization` / `pipeline-stage-runtime` (graph-stage conformance) → the conversion-stack rename → the migrations.

## Decisions

### Decision: Identity is a stable surrogate; content is an observable column

**Chosen:** A derived row's identity is a **stable surrogate** assigned once at persistence — for `chunk_id`, a UUID (uuid7 preferred for index locality).
The content fingerprint (`content_hash`) remains a separate observable column.
Cross-generation reuse is a `UNIQUE` constraint on the producer's **sameness-key** — for chunks, `(source_id, heading_path, ordinal, content_hash)` — so persistence reuses the existing surrogate when the sameness-key matches and assigns a new one otherwise.

**Rationale:** Decouples the three jobs content-addressing had conflated.
Identity stability no longer depends on any producer's determinism (it's assigned, not computed), which is what makes the rule apply uniformly to stochastic producers.
Portability is structural: a UUID surrogate carries no database-local sequence state, and nothing hashes it.
Change-detection survives via the `content_hash` column ("content edit vs. structural move" is a column comparison, not an identity comparison).
The sameness-key reuse must be the **full** `(source_id, heading_path, ordinal, content_hash)` tuple — keying on `content_hash` alone would collapse "same content, different address" into one identity, violating chunking's identity scenarios.

**Alternatives considered:**

- **Keep `chunk_id` content-addressed (a hash).**
  Rejected: it conflates identity with change-detection, and — for the eventual mention/entity producers — bakes determinism assumptions into identity.
  The whole change exists to stop this.
- **Autoincrement integer surrogate.**
  Acceptable and compact, but a sequence is database-local; a UUID avoids any cross-backend sequence-coordination question and is unambiguously portable.
  Either migrates as data; UUID is the safer default for a value downstream FKs and a future Postgres backend depend on.
- **Reuse keyed on `content_hash` alone.**
  Rejected: collapses distinct addresses with identical content.

### Decision: The derivation key is semantic-only and embeds the upstream key

**Chosen:** A run's `derivation_key` is a hash over **semantic inputs only** — content fingerprints, producer/prompt/model/config versions, and the **upstream run's `derivation_key`** — canonically serialized.
It SHALL NOT include any database-local identifier (`run_id`, autoincrement row ids, `conversion_output_id` surrogates, and — once `chunk_id` becomes a surrogate — `chunk_id` itself: the contextualization variant derivation/memo keys embed each chunk's portable content key, the sameness-key fingerprint, not its surrogate identity).
What a run consumed is recorded separately as explicit provenance pointers.

**Rationale:** Embedding the upstream `derivation_key` (not the upstream run's local id) is what makes invalidation **propagate** down the chain automatically and **portably**: an upstream input change flips the upstream key, which flips this key, which supersedes this run — and the same logical content computes the same key on any backend.
Keeping pointers separate from the key preserves "what did I actually read" (legible, verifiable) distinct from "would I redo this" (the matching fingerprint).
This is the existing chunk/contextualization pattern, named once here as the cross-cutting rule.

**Alternatives considered:**

- **Embed the upstream `run_id` in the derivation key.**
  Rejected — non-portable; identical content on Postgres would compute a different key and falsely supersede. (This was the latent defect the mention design would have shipped.)
- **A bespoke per-stage consumed-input fingerprint** (as the mention draft proposed).
  Rejected — it is the derivation key computed at the wrong scope; the standard key subsumes it.

### Decision: Idempotency is a data-model invariant at the run level, with row-level UNIQUE where a sameness-key exists

**Chosen:** Run-level idempotency is the existing partial unique index ("one active run per `(stage, scope_id)`") plus reuse-on-matching-`derivation_key`.
Where a producer yields a stable sameness-key, a `UNIQUE` constraint on it backs **row-level** non-duplication in the database.
Where a producer is stochastic (no stable sameness-key — contextualization variants, future mentions), row-level non-duplication rests on run-level reuse **plus atomic per-unit writes** (the contextualization stage's existing per-document transaction + output memo).

**Rationale:** Pushes the guarantee into the schema (declarative, survives bugs and the planned Postgres multi-writer backend) rather than trusting application code — but only where a _stable_ sameness-key exists.
A stochastic producer cannot have a meaningful row-level sameness-key (its discriminators wobble), so forcing one would be wrong; run-level reuse + atomic writes is the correct guarantee there.
This is why the `pipeline-identity` idempotency requirement is partitioned run-level / deterministic-row / stochastic-row.

**Alternatives considered:**

- **"Idempotency is only step behavior, nothing in the data model."**
  Rejected — a TOCTOU race between concurrent backfill workers (or under Postgres) yields duplicates a `UNIQUE` constraint would reject.
  Don't surrender the database guarantee.
- **Force a synthetic sameness-key onto stochastic rows.**
  Rejected — the discriminators aren't stable, so the constraint would either reject legitimate rows or fail to dedup.

### Decision: Invalidation is lazy by default; large reprocessing is gated by human confirmation

**Chosen:** A producer-version change makes prior generations **logically stale** (detectable by comparing a row's recorded version to the current) without eager recompute; a stale-but-active generation stays usable until touched.
Recompute happens lazily on access or via an explicit operation.
Any user-initiated reprocessing with large downstream blast radius — a corpus-wide backfill, or a base-document edit that cascades the derivation graph — SHALL require an explicit human confirmation (warn + approve) before running.
The confirmation is **surface-agnostic** (any entry point that initiates such an op) and **does not compute a cost**.
Each derived row records its producer version, so a version-heterogeneous corpus is valid and any version's coverage is queryable.

**Rationale:** A prompt/model version bump can imply a multi-thousand-dollar corpus re-contextualization; eager invalidation would make a version bump a wallet event.
Decoupling staleness (a cheap key comparison) from recompute (a deliberate, gated action) keeps version bumps free by default.
Per-row version stamps make the resulting mixed-version corpus safe because heterogeneity is _recorded_, not silent — a homogeneous slice is always queryable for clean measurement.

**Alternatives considered:**

- **Eager corpus recompute on version bump.**
  Rejected — unbounded, surprise cost; the originating concern.
- **A cost estimate in the gate.**
  Deferred — the requirement is a human-in-the-loop warning + approval; a precise estimate is a later refinement, not required for the guarantee.

### Decision: Conformance is referenced, not restated (single source of truth)

**Chosen:** The cross-cutting rules (surrogate identity, semantic derivation key, lazy invalidation) live **once** in `pipeline-identity`.
Conforming stages do not restate them.
Concretely, `chunk-contextualization`'s delta only renames `aizk_uuid` → `source_id`; its lazy-invalidation and surrogate-identity conformance are inherited from `pipeline-identity` and **exercised by tasks** (a `context_version` bump marks variants stale without eager recompute; a corpus-wide re-contextualization hits the human-confirmation gate), not duplicated as `chunk-contextualization` requirements.

**Rationale:** Duplicated contract text drifts.
The expensive-producer behavior is the _same_ contract; it is verified once in `pipeline-identity` and demonstrated on the concrete stage by tests.
This is the document-hierarchy / contract-floor discipline applied to ourselves.

**Alternatives considered:**

- **Restate the lazy/identity rules in each conforming capability.**
  Rejected — duplication and drift; the value ceiling (a requirement that merely echoes a cross-cutting one serves no distinct story).

### Decision: Canonical `source_id`, including the breaking public-API rename

**Chosen:** `source_id` is the single canonical role-name for the durable source identity, replacing `aizk_uuid` (and `doc_id`) everywhere — data model, provenance, and the **public conversion API** (`GET /v1/bookmarks/{source_id}/outputs`, the job-list `source_id` filter).
`scope_key` → `scope_id` in the run primitive.
The `_id` / `_key` / `_hash` suffix convention is specified as a SHOULD so a name communicates whether its value is a pointable identity, a matching fingerprint, or a content fingerprint.

**Rationale:** One role-based name kills the gratuitous `doc_id`/`scope_key` divergence.
`source_id` is role-named (vs the type-leaky `aizk_uuid`).
The API break is acceptable because the system is pre-release with no external consumers — paying it now (greenfield) avoids a deprecation window later.

**Alternatives considered:**

- **Keep `aizk_uuid` canonical; rename only the downstream aliases (non-breaking).**
  Rejected by the owner: greenfield is the moment to adopt the role-name everywhere.
- **`source_id` internally, `aizk_uuid` as a justified API boundary alias.**
  Rejected for the same greenfield reason — no need to carry a second name.

### Decision: `source_ref_hash` is the dedup/sameness key, not the identity (precision)

**Chosen:** `pluggable-pipeline`'s prose is corrected so `source_ref_hash` is named the Source **dedup/sameness key** (a content fingerprint that determines which submissions resolve to the same `source_id`), not "Source identity."
No token is renamed and no migration is required — `source_ref_hash` keeps its correct `_hash` name; only the "identity" prose changes.

**Rationale:** Conversion already implements the target pattern (surrogate identity `source_id` + content-hash sameness-key `source_ref_hash`); the spec prose merely mislabeled it.
Correcting the prose is the change's own thesis applied to itself.

## Migration Plan

This change affects data and contracts, so per governance it carries a migration plan; the deprecation schedule is **trivial (immediate cutover, no dual-support window)** because there are no external consumers.

**Database (one Alembic revision, ordered):**

1. **`chunk_id` surrogate.**
   Add a surrogate `chunk_id` value space (UUID); for existing rows, assign a surrogate per distinct `(source_id, heading_path, ordinal, content_hash)` and record the old-hash → new-surrogate map; repoint every reference to `chunk_id` (contextualization variants, chunk-run manifests, and the `graph_content_fts` content-index column) through the map; add `UNIQUE(source_id, heading_path, ordinal, content_hash)`; swap the PK. (Greenfield: data volume is dev-only, but the migration is written to be correct for populated databases.)
2. **`aizk_uuid` → `source_id`.**
   Rename the `Source` identity column and every dependent FK (`ConversionJob`, `ConversionOutput`, graph-stage rows) in one revision.
3. **`scope_key` → `scope_id`.**
   Rename the column on the run/event tables of the run primitive.

Within the single revision the surrogate step is emitted **after** the column renames so it references the final `source_id` column directly; the revision's end-state is independent of that internal statement order.
Because the prior `chunk_id` was already `xxh64(source_id, heading_path, ordinal, content_hash)`, every existing row already carries a distinct sameness-key, so "a surrogate per distinct sameness-key" is one surrogate per row; the `chunk_id` PK column is unchanged structurally (only its values become surrogates) and the sameness-key uniqueness is a named unique index (`ix_graph_chunks_sameness_key`) so `create_all` and the migration match without a table rebuild.
The surrogate is minted with `uuid4` — matching the conversion stage's `source_id` surrogate and adding no dependency; uuid7's index-locality is an unmeasured preference, deferred.

The `schema-migrations` ORM-vs-migration equivalence test covers structural fidelity after the revision.

**Public API (breaking):** rename the `{aizk_uuid}` path parameter, the `aizk_uuid` job-list query parameter, and `aizk_uuid` schema fields to `source_id`.
Capture the OpenAPI `before/` snapshot; `schemas/expected.md` records the rename diff; the post-change snapshot reflects `source_id`.

**Non-requirement spec sweep (tasks):** rewrite `aizk_uuid`/`scope_key` in non-requirement sections that MODIFIED blocks cannot reach — the conversion specs' Technical Notes (S3 layout `s3://…/<source_id>/`, route list, idempotency-key note), `chunk-contextualization`'s run note (`str(source_id)`), and `pipeline-stage-runtime`'s Purpose line (`scope_id`).

## Architecture

```text
The five identifier roles, and how a derived row is identified, made idempotent, and traced:

  source_id (spine) ──────────────────────────────────────────────┐
        │  durable identity of the source; one canonical name      │
        ▼                                                           │
  run = pipeline_runs(stage, scope_id=str(source_id))               │
        │  one ACTIVE per (stage, scope_id)  [data-model invariant] │
        │  derivation_key = H(versions, config, content hashes,     │
        │                     UPSTREAM derivation_key)              │  provenance
        │     ▲ no DB-local id  → portable + auto-propagating       │  pointers
        ▼     └────────────── upstream run's derivation_key ────────┘  (consumed
  derived row                                                          run/row refs)
    identity  = stable surrogate (UUID; assigned once)  ── never content, never DB-local
    sameness  = UNIQUE(sameness-key)  [deterministic]  | run-reuse + atomic write [stochastic]
    content   = content_hash column  (observable; change-detection)
    version   = producer version stamp  (lazy staleness; mixed-version corpus is valid)

  Invalidation: version bump → derivation_key changes → logically stale (NOT eager recompute).
                recompute = lazy on access OR explicit op gated by human confirmation.
```

## Risks

- **`chunk_id` FK repointing is the riskiest migration step.**
  Contextualization variants and chunk-run manifests reference `chunk_id`; the surrogate swap must repoint them through the old→new map without orphaning.
  Mitigation: a migration test that asserts referential integrity (no dangling FK) and round-trips a populated fixture; the proposal's verify-then-spec gate (confirm shared-row reuse works under the new `UNIQUE` before relying on it).
- **Natural-key reuse keyed on too little would collapse identities.**
  Mitigation: the chunking delta's three identity scenarios (same/same, same-address/diff-content, diff-address/same-content) are the regression guard.
- **Portability cannot be exercised against a real Postgres backend (not built).**
  Mitigation: verify the _proxy_ invariant — `chunk_id` and `derivation_key` are deterministic functions of semantic content with **no DB-local input** (a unit test that varying surrogate/run values does not change a computed key or identity).
  The Litestream restore path is testable for SQLite.
- **Stale-but-active rows consumed downstream.**
  Mitigation: per-row version stamps + queryable coverage; downstream may accept the mix or slice a homogeneous version.
- **Breaking API rename.**
  Mitigation: pre-release, no consumers; OpenAPI snapshot + `expected.md` make the diff explicit.
