# Proposal: Identity & Provenance Foundation

## Intent

Each pipeline stage has grown its own vocabulary and its own mechanisms for the two promises every stage must keep — **idempotency** (re-running produces the same result, once) and **provenance** (every derived row traces back to exactly what produced it).
The drift makes the pipeline hard to reason about and invites subtle, stage-specific correctness bugs: a non-portable surrogate smuggled into a content-addressed id, idempotency enforced only in application code, or the same source identity wearing three different column names.
Before the next stage (mention extraction) is built on this substrate, standardize the conventions so every stage — current and future — keeps both promises the same way, enforced where possible by the database rather than by per-stage code.

## User Stories

### Story: coherent-pipeline-foundation

As a developer adding a pipeline stage, I want one documented identity-and-provenance grammar that every stage follows, so that the duplicate-free, portable, recoverable guarantees hold uniformly across current and future stages instead of being re-derived (and subtly broken) per stage.

### Story: idempotent-duplicate-free-pipeline

As an operator, I want each stage's unit of work to be idempotent — re-running it under retries or re-ingestion yields the same result without duplicates or corruption — with a run-level uniqueness invariant in the database as its backbone, so that my knowledge graph stays duplicate-free through re-ingestion and recovery.

### Story: portable-knowledge

As an operator, I want derived-data identities and the references between them to survive a database migration or a restore from backup, so that I can move backends (e.g. SQLite to Postgres) or recover from a snapshot without dangling references or a broken graph.

### Story: affordable-pipeline-evolution

As an operator, I want any expensive reprocessing — a model or prompt version bump, or a base-document edit that cascades through the derivation graph — to be lazy by default and to require an explicit human-in-the-loop confirmation before it runs, so that improving or editing the pipeline never silently triggers a surprise large-scale reprocessing job.

## Scope

Capabilities in build-dependency order: the cross-cutting **`pipeline-identity`** contract first (it defines the grammar and rules every stage conforms to), then the **`chunking`** and **`chunk-contextualization`** conformance migrations that bring the two graph-stage producers onto it, then the `scope_key` → `scope_id` rename in the run primitive (**`pipeline-stage-runtime`**), and finally the **`source_id`** vocabulary rename across the conversion stack (**`conversion-api`**, **`conversion-worker`**, **`graph-jobs-ui`**, **`conversion-ui`**).

**In scope:**

- **`pipeline-identity` capability (new).**
  A cross-cutting contract that every stage producing derived, persisted state conforms to:

  - **Canonical source-identity vocabulary.**
    One canonical name — `source_id` — for the durable source identity across the data model and public interfaces.
    A reusable engine/infrastructure layer MAY use a role-generic alias (e.g. the run primitive's `scope_id`) only when it is genuinely stage-agnostic AND it documents the mapping at the boundary.
    Minting any new identifier or provenance-pointer name must state which role it plays, why an existing name does not fit, and how it maps back to the spine.
  - **Identifier suffix convention (SHOULD).**
    Names ending `_id` denote identities (pointable handles), `_key` denotes a computed matching fingerprint, and `_hash` a content fingerprint — so a name communicates its role.
    A run's scope reference is an identity and is therefore `scope_id`, not `scope_key`.
  - **Surrogate identity rule.**
    A persisted derived row's identity is a stable surrogate (assigned once, never recomputed), never a content hash and never embedding a non-portable surrogate (such as a local run id).
    Content hashes and fingerprints survive only as **observable columns** for change-detection and provenance — never as the identity.
  - **Idempotency as a run-level data-model invariant.**
    Idempotency is enforced at the run level by a uniqueness invariant — at most one active run per `(stage, scope_id)`, with re-invocation reusing the active run when its `derivation_key` matches — not left solely to step code.
    Where a producer yields a stable sameness-key, a defensive `UNIQUE` constraint on it backs row-level idempotency in the database; where the producer is stochastic (no stable sameness-key), row-level idempotency rests on run-level reuse plus atomic per-unit writes.
  - **Provenance grammar.**
    Provenance is carried by the `derivation_key` (a fingerprint of semantic inputs + producer versions + config, embedding the upstream `derivation_key`, never local surrogate ids) plus explicit consumed-run/row pointers — distinct from both identity and the idempotency key.
  - **Lazy invalidation by default.**
    A producer-version change marks prior generations logically stale but does not eagerly recompute them.
    Recompute happens lazily on access, or via an explicit operation that — like any user-initiated reprocessing with large downstream blast radius (a corpus backfill, or a base-document edit that cascades through the derivation graph) — is gated by a human-in-the-loop confirmation before it runs.
    The confirmation need not compute a precise cost; it must warn and require explicit approval.
    Generation coverage is queryable, and a version-heterogeneous corpus is tolerated because each row records the version that produced it.

- **`chunking` capability (conformance, MODIFIED).**
  Migrate `chunk_id` from a content-addressed identity to a stable surrogate; retain `content_hash` as an observable change-detection column (so "content edit vs. structural move" stays independently observable); rename the source-identity column to `source_id`.

- **`chunk-contextualization` capability (conformance, MODIFIED).**
  Rename source-identity vocabulary to `source_id` in the requirements that name it.
  Lazy-invalidation and surrogate-identity conformance are **inherited from `pipeline-identity`, not restated here** (single source of truth) — justified in `design.md` and exercised by `tasks.md` (a `context_version` bump marks variants stale without eager recompute; a corpus-wide re-contextualization hits the human-confirmation gate).

- **`pipeline-stage-runtime` (conformance, MODIFIED).**
  Rename the run primitive's scope reference `scope_key` → `scope_id` (the `_id`-for-identity convention; non-breaking, internal), updating the `(stage, scope_id)` contract and the run/event columns that name it.

- **Conversion-stack vocabulary rename (`conversion-api`, `conversion-worker`, `graph-jobs-ui`, `conversion-ui`, MODIFIED).**
  Rename `aizk_uuid` to `source_id` wherever the contract names the durable source identity — including the **public API surface** (`GET /v1/bookmarks/{source_id}/outputs`, the job-list `source_id` filter) and the `Source` identity columns.
  This is a **breaking API change** and a `Source`-column data migration, accepted pre-release (no external consumers); the migration plan and the now-trivial deprecation note live in `design.md`.
  Non-requirement sections that still name the identity are swept by tasks (they are not expressible as MODIFIED requirement blocks): the conversion specs' Technical Notes (S3 layout `s3://…/<aizk_uuid>/`, route list, idempotency-key note), `chunk-contextualization`'s run note (`str(aizk_uuid)`), and `pipeline-stage-runtime`'s Purpose line (`scope_key`).

- **`pluggable-pipeline` (precision fix, MODIFIED).**
  Carries no `aizk_uuid` token, so it is not part of the rename.
  But its `source_ref_hash` requirement calls the hash "Source identity" / a "versioned identity contract" — the exact hash-is-identity conflation this change exists to kill.
  One requirement is reworded to name `source_ref_hash` the Source **dedup/sameness key** (a content fingerprint that determines which submissions resolve to the same `source_id`), distinct from the durable identity.
  No token rename, no migration.

- **Migrations** (Alembic) for the `chunk_id` type/PK change, the sameness-key uniqueness backing, FK updates, and the `aizk_uuid` → `source_id` column renames across the `Source` table and every dependent FK.

**Out of scope (named, separate changes):**

- **`mention-extraction-foundation` itself** — it is revised separately to conform once this lands; this change is its prerequisite.
- **The Postgres backend implementation** — this change only guarantees identities and references are _portable_ to it; building the second backend is `pluggable-database`'s downstream peer change.
- **Rebuilding the conversion stage's existing job idempotency key** — align its source-identity vocabulary, but do not redesign its idempotency mechanism.
- **A general operator console for reprocessing** — this change specifies the surface-agnostic human-in-the-loop confirmation contract and wires it into existing reprocessing entry points; it does not build a new UI.
- **New orchestration technology, object-storage changes, or retrieval/graph features.**

## Approach

> Mechanism sandbox; contracts live in the delta specs, chosen mechanisms formalize in `design.md`.

- **The grammar is the deliverable.**
  The five identifier roles (source identity, run, derivation key, output identity, provenance pointers) and the rules above are written once as the `pipeline-identity` contract; the two shipped graph producers are migrated to conform, demonstrating substitutability and giving the next stage a pattern to copy rather than re-invent.
- **Identity becomes a surrogate; content becomes a column.**
  `chunk_id` moves to a surrogate PK with `content_hash` retained as an indexed observable column; the run-independent shared-chunk-row reuse currently keyed on the content-addressed id is re-expressed as a `UNIQUE(content_hash)` lookup-and-reuse.
  The early-cutoff/change-detection behavior the chunking spec relies on is preserved through the column, not the id.
- **Idempotency lives in the schema.**
  The run primitive's existing single-active-run uniqueness is named as the idempotency invariant; row-level defensive `UNIQUE` constraints are added wherever a stable sameness-key exists.
  This keeps the guarantee under the planned Postgres multi-writer backend, where a single-serialized-writer assumption no longer holds.
- **Invalidation is decoupled from recompute.**
  Staleness is a cheap derivation-key comparison; recompute is a separate, deliberate action — lazy on access, or an explicit operation gated by a human-in-the-loop confirmation whenever the blast radius is large (a corpus backfill, or a base-document edit that cascades through the derivation graph).
  The gate warns and requires approval; it need not estimate cost.
  Per-row version stamps make a mixed-version corpus safe and measurable.
- **Rename is a mechanical sweep across every stage.**
  `source_id` replaces `aizk_uuid`/`doc_id` everywhere the durable source identity is named — columns, kwargs, fixtures, notebooks, docs — across all stages including conversion (where the identity is canonically assigned), with `scope_key` retained as the engine alias plus its mapping note.
  The canonical-vocabulary requirement lives once in `pipeline-identity`; a baseline spec gets a MODIFIED delta only where its contract text names the identity, and the mechanical code/fixture sweep is carried by tasks.

## Open Questions

> Resolved during delta-spec drafting: `pipeline-stage-runtime` already states the one-active-run-per-`(stage, scope)` invariant, so `pipeline-identity` references it (and adds the `derivation_key`-reuse half); the runtime delta only renames `scope_key` → `scope_id`.

- **`chunk_id` reuse under a surrogate.**
  Verify the run-independent shared-chunk-row reuse works cleanly under `UNIQUE(content_hash)` + reuse-lookup before migrating, including FK behavior on the contextualization variants that reference `chunk_id`.

## Schema Impact

**OpenAPI (`conversion-api`):** Changes.
The `aizk_uuid` path parameter (`/v1/bookmarks/{aizk_uuid}/outputs`), the `aizk_uuid` job-list query parameter, and `aizk_uuid` schema fields are renamed to `source_id`.
The `before/` snapshot is captured from the committed baseline `.specs/schemas/conversion-api-openapi.json`; `schemas/expected.md` records the rename diff; the post-change snapshot reflects `source_id`.
This is a **breaking change**, accepted pre-release with no external consumers.

**Database (SQLite):** Adds an Alembic migration that (a) changes `chunk_id` from a content-addressed string PK to a stable surrogate, with `(source_id, heading_path, ordinal, content_hash)` uniqueness backing cross-generation reuse; (b) renames the `Source` identity columns and every dependent foreign key from `aizk_uuid` to `source_id`; and (c) updates dependent graph-stage columns.
These tables are **not** tracked by `.specs/.sdd/schema-config.yaml`, so no DB snapshot is captured; the `schema-migrations` capability's ORM-vs-migration equivalence test covers structural fidelity.
Concrete table shapes and migration ordering are decided in `design.md`.
