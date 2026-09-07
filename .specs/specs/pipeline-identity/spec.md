# Pipeline Identity Specification

> Synced from change `output-sensitive-identity` on 2026-09-06

## Purpose

The pipeline-identity capability defines the cross-stage identity-and-provenance grammar every processing stage conforms to: how a derived row is named, identified by a stable surrogate, made idempotent at the run level, and traced back to its source through a semantic derivation key and explicit provenance pointers — with invalidation lazy by default and large reprocessing gated by human confirmation.
It is the contract floor that deterministic and stochastic producers alike satisfy, so stages stay substitutable and derived knowledge stays portable across backends.

## Requirements

### Requirement: Every identifier belongs to exactly one tier of the identifier taxonomy

Every internal persisted identifier SHALL belong to exactly one of five tiers: a **surrogate locator** (a database-local row reference), a **semantic identity** (a stable, pointable handle to a source, row, or scope), a **matching key** (a computed fingerprint used for equality-based reuse or deduplication — idempotency keys, sameness keys, occurrence keys), a **derivation key** (a fingerprint of a stage's consumed inputs), or a **content hash** (a fingerprint of raw content).
An external system's identifier is a provenance attribute value, not an internal identifier, and is outside the taxonomy.
A semantic identity SHALL be UUID-typed wherever it is persisted or exchanged; no identity column is string-typed.
A UUID identity serialized in canonical form inside a declared JSON locator (a documented boundary) is conformant.
A matching key SHALL NOT be used as a pointable identity.
A surrogate locator SHALL NOT appear in a derivation key, a content hash, or any portability claim; it MAY serve as an API resource handle.

#### Scenario: An identity column is UUID-typed

- **GIVEN** a persisted column that names a semantic identity (for example `source_id`, `chunk_id`, `mention_id`)
- **WHEN** its declared type is inspected
- **THEN** it is a UUID type, not a string

#### Scenario: The engine's scope alias is typed as the identity it holds

- **GIVEN** the run primitive's role-generic `scope_id`, whose values are semantic identities
- **WHEN** its declared type is inspected
- **THEN** it is a UUID type, and a stage whose scope is not naturally a UUID mints a scope identity rather than storing a non-UUID string

#### Scenario: A matching key is not a pointable identity

- **GIVEN** a computed matching fingerprint (an idempotency, sameness, or occurrence key)
- **WHEN** rows are referenced across the data model
- **THEN** no reference resolves rows through the matching key; pointable references use identities or locators

#### Scenario: A surrogate locator never leaks into a portable value

- **GIVEN** a derivation key, content hash, or cross-database portability claim
- **WHEN** its inputs are inspected
- **THEN** no database-local row reference appears among them

### Requirement: The durable source identity has one canonical name

The durable source identity SHALL be named `source_id` wherever it appears as a contract term in the data model, public interfaces, and provenance.
A reusable engine or infrastructure layer MAY refer to it through a role-generic alias (for example a run primitive's `scope_id`) only when that layer is genuinely stage-agnostic AND the alias's mapping to `source_id` is documented at the boundary.
Introducing any new identifier or provenance-pointer name SHALL state the role it plays, why an existing name does not fit, and how it resolves back to `source_id`.

#### Scenario: The source identity is named source_id where it is a contract term

- **GIVEN** a stage that names the durable source identity in its persisted data model or its public interface
- **WHEN** that name is inspected
- **THEN** it is `source_id`, not a stage-specific synonym

#### Scenario: An engine alias is permitted with a documented boundary mapping

- **GIVEN** a stage-agnostic engine layer that scopes its records by a role-generic key rather than by `source_id` directly
- **WHEN** that alias is inspected
- **THEN** its mapping to `source_id` is documented at the boundary, and the alias is used only within that engine layer

### Requirement: External identifiers are provenance attributes, never internal identity

When a source is imported from an external system that carries its own identifier, the internal `source_id` SHALL be a newly minted identity, and the external identifier SHALL be recorded as a provenance attribute on the source.
The external identifier SHALL remain resolvable for correlation; it SHALL NOT be adopted as, or embedded in, any internal identity.

#### Scenario: An imported source mints its own identity

- **GIVEN** a source imported from an external bookmark system carrying that system's identifier
- **WHEN** the source row is inspected
- **THEN** its `source_id` is an internally minted identity, and the external identifier is present as a provenance attribute

#### Scenario: The external identifier stays correlatable

- **GIVEN** a source imported with an external identifier
- **WHEN** the external system's record must be matched to the internal source
- **THEN** the match resolves through the recorded provenance attribute, not through any internal identity

### Requirement: Identifier names signal identity versus fingerprint by suffix

An identifier that names an identity — a stable, pointable handle to a row, entity, or scope — SHOULD use the `_id` suffix; a computed matching fingerprint SHOULD use the `_key` suffix and a content fingerprint the `_hash` suffix, so that a name communicates whether its value is a pointable identity or a derived fingerprint.
A run's scope reference is an identity and is therefore named `scope_id`, not `scope_key`.

#### Scenario: Suffixes distinguish identities from fingerprints

- **GIVEN** an identifier that names a pointable identity and one that names a computed fingerprint
- **WHEN** their names are inspected
- **THEN** the identity uses the `_id` suffix and the fingerprint uses the `_key` or `_hash` suffix

### Requirement: A derived row's identity is a stable, portable surrogate

The identity of a persisted, derived row SHALL be a stable surrogate: assigned once at persistence, never recomputed, not encoding the row's content, and not embedding any database-local identifier (such as a run's local id).

A row's **sameness-key** is the set of fields a producer uses to decide whether two produced rows are the same logical row for reuse.
A producer SHALL reuse an existing identity across exactly the re-derivations over which it can reproduce that sameness-key, and SHALL mint a new identity otherwise — so the key's scope follows the producer's reproducibility:

- reproducible independent of any run → the key is run-independent and the identity is reused across runs;
- reproducible only within a run → the key includes the **producing-run reference** (a reference to the run that produced the row, e.g. its `run_id`) and the identity is reused within that run but not across a superseding run;
- not reproducible even within a run → there is no stable sameness-key, and each superseding run mints new identities.

A producer is **stochastic** when its output content varies across invocations on identical inputs (for example an LLM); such a producer is reproducible at best within a run, so it falls in the second or third case above, never the first.

The surrogate is the durable reference; the sameness-key governs reuse only — it is never the identity and never a `derivation_key`.
Unlike the identity and the `derivation_key` (which embed no database-local identifier), a sameness-key MAY include a producing-run reference when reuse is intentionally run-scoped.
The identity SHALL remain valid and resolvable after a database migration or restore; likewise, any producing-run reference in a persisted sameness-key SHALL remain resolvable — preserved or explicitly repointed in the migration — so run-scoped reuse stays correct.

#### Scenario: A derived row's identity survives a backend migration

- **GIVEN** a persisted derived row and the references to it from other rows
- **WHEN** the database is migrated to another backend or restored from a snapshot
- **THEN** the row's identity and every reference to it resolve unchanged, with no dangling reference

#### Scenario: Re-deriving a row with the same sameness-key reuses its identity

- **GIVEN** a derived row persisted by a producer that defines a sameness-key
- **WHEN** a later generation re-derives a row with that same sameness-key
- **THEN** the row resolves to the same identity it already had, and that identity was not recomputed from the row's content

#### Scenario: A producer without a sameness-key mints new identities under a superseding run

- **GIVEN** a stochastic producer that defines no stable sameness-key for its rows
- **WHEN** it re-derives its outputs for a scope
- **THEN** the new outputs receive new identities under a new run that supersedes the prior one, and no prior identity is mutated

### Requirement: A database rebuild remints identities and correlates through source facts

A **rebuild** deletes the database and reproduces derived state by re-ingesting raw inputs and replaying stages; it is distinct from a migration or restore, which preserves rows.
After a rebuild, no internal identity is required to match its pre-rebuild value.
Correlation of a logical source across a rebuild SHALL be achievable through its source metadata and content fingerprints, which SHALL remain queryable.
A derivation key whose inputs include an internal identity — for example a chunk key scoped by `source_id` — re-derives to a new value after a rebuild, so it is a portability handle across a migration but not across a rebuild.
No consumer outside the system SHALL durably depend on an internal identity surviving a rebuild.

#### Scenario: A migration preserves identities

- **GIVEN** a database migrated to another backend or restored from a snapshot
- **WHEN** identities and references are inspected
- **THEN** they resolve unchanged (the existing surrogate contract)

#### Scenario: A rebuild remints identities but preserves correlation

- **GIVEN** the same raw inputs ingested before and after a rebuild
- **WHEN** the two databases are compared
- **THEN** internal identities differ, and each logical source is matchable across the two by its source metadata and content fingerprints

### Requirement: Content fingerprints are observable columns, never identity

A producer that needs change-detection or content provenance SHALL expose the content fingerprint (for example a `content_hash`) as a separately observable column, distinct from the row's identity.
Whether a derived input changed SHALL be determinable by comparing that observable column, not by comparing or recomputing identities.
A stage whose output is consumed by a downstream stage SHALL persist an observable fingerprint of that consumable output at production time, so downstream currentness is classifiable by comparing persisted fingerprints without re-reading or re-hashing content.

#### Scenario: A content change is observable without the identity carrying it

- **GIVEN** the same logical row derived under two generations, once from changed content and once from unchanged content
- **WHEN** the two generations' rows are compared
- **THEN** the content change is visible by comparing the observable content-fingerprint column, while the row's identity remains a stable surrogate that does not itself encode the content

#### Scenario: Downstream classification reads persisted fingerprints

- **GIVEN** an upstream generation with a persisted output fingerprint and a downstream run over it
- **WHEN** the downstream source's currentness is classified
- **THEN** the classification compares persisted fingerprint columns and does not load or re-hash the upstream content

### Requirement: Idempotency is a run-level data-model invariant

A stage's derived outputs SHALL belong to a run for which at most one run per `(stage, scope_id)` is active, recorded and superseded atomically (the run primitive's invariant this capability builds on).
Re-invoking a stage for a scope whose `derivation_key` matches the active run SHALL reuse that run and produce no new run and no duplicate outputs.
Where a producer yields a stable sameness-key for its rows, the data store SHALL reject a duplicate row for that key; where a producer is stochastic and yields no stable sameness-key, row-level non-duplication SHALL rest on run-level reuse together with atomic per-unit writes.

#### Scenario: Re-invocation with unchanged inputs reuses the active run

- **GIVEN** an active run for a `(stage, scope_id)` recorded under a given `derivation_key`
- **WHEN** the stage is re-invoked for that scope with the same `derivation_key`
- **THEN** the existing active run is reused, and no new run and no duplicate outputs are created

#### Scenario: A deterministic producer's duplicate row is rejected by the store

- **GIVEN** a producer that yields a stable sameness-key for its rows
- **WHEN** a row with an already-present sameness-key is written again
- **THEN** the data store rejects the duplicate rather than creating a second row

#### Scenario: A stochastic producer's re-executed unit does not duplicate

- **GIVEN** a stochastic producer whose unit of work is written atomically under a run
- **WHEN** the unit is re-executed within the same active run after a failure that committed nothing
- **THEN** the unit's outputs are present exactly once, with no partial or duplicated rows

### Requirement: Applicability records extend a run's validity to equivalent later inputs

When an upstream generation changes and the candidate key for the new inputs under the currently-resolved stage configuration equals the active run's `derivation_key`, the stage SHALL record an append-only **applicability** relation from the active run to the new inputs, without superseding the run and without invoking the producer.
An applicability relation SHALL be valid only when its referenced inputs exist, carry the expected role, belong to the run's source scope, and match the recomputed candidate key, and only when the run's outputs are present and complete; an invalid relation SHALL be rejected.
Writing an identical relation twice SHALL be idempotent.
A run referenced by a retained provenance or applicability record SHALL remain resolvable; it is not eligible for compaction while the reference is retained.
Applicability is distinct from provenance: the original provenance pointers remain immutable and continue to identify the exact inputs the producer consumed.

#### Scenario: Equivalent regeneration reuses the run without producer invocation

- **GIVEN** an active downstream run and a new upstream generation whose consumed data is byte-equivalent to what the run consumed, with the stage configuration unchanged
- **WHEN** the stage reconciles the source
- **THEN** an applicability relation to the new inputs is recorded, the run stays active, its provenance is unchanged, and the producer is invoked zero times

#### Scenario: A changed input produces a new generation instead

- **GIVEN** an active downstream run and a new upstream generation whose consumed-data fingerprint differs
- **WHEN** the stage reconciles the source
- **THEN** no applicability relation is written, and a new generation is produced under the applicable confirmation rules

#### Scenario: An invalid relation is rejected

- **GIVEN** a candidate applicability relation whose inputs are missing, cross-scope, wrong-role, or fingerprint-mismatched
- **WHEN** the relation is written
- **THEN** the write is rejected and no partial relation is recorded

#### Scenario: Re-recording a relation is idempotent

- **GIVEN** an applicability relation already recorded for a run and input set
- **WHEN** the identical relation is written again
- **THEN** exactly one relation exists and the write reports success

#### Scenario: An incomplete run is not reused

- **GIVEN** an active run whose owned outputs are missing or incomplete, and equivalent current inputs
- **WHEN** the stage reconciles the source
- **THEN** no applicability relation is recorded, and the source is eligible for regeneration

#### Scenario: A concurrent change aborts the reconciliation

- **GIVEN** a reconciliation planned against inputs that change — or a target run that is superseded — between its read and its write
- **WHEN** the write-phase validation runs
- **THEN** the outcome is a retryable stale-plan result, and neither an applicability relation nor a new generation is committed

### Requirement: Currentness is classified from stored relations alone

For a stage and source with an active run, currentness SHALL be classified from persisted state without invoking any producer, against the **candidate key**: the derivation key computed from the currently active inputs and the currently-resolved stage configuration — never from the configuration stored on the run under evaluation.
The classes are: **current** when an applicability relation (or the original provenance) links the run to the currently active inputs; **needs-reconciliation** when no such relation exists but the candidate key equals the active run's key; **stale** when the candidate key differs, whether the divergence is in consumed data or in stage configuration.
A needs-reconciliation source is eligible for the cheap reconciliation path; a stale source is eligible for regeneration under the applicable confirmation rules.
No classification SHALL admit work automatically.

#### Scenario: A linked run is current

- **GIVEN** an active run linked to the currently active inputs by provenance or applicability
- **WHEN** currentness is classified
- **THEN** the source is current and no work is indicated

#### Scenario: An unlinked but equivalent run needs reconciliation

- **GIVEN** an active run with no relation to the current inputs, whose key equals the candidate key for the current inputs and currently-resolved configuration
- **WHEN** currentness is classified
- **THEN** the source is classified needs-reconciliation, and no producer is invoked by the classification

#### Scenario: A diverged run is stale

- **GIVEN** an active run whose key differs from the candidate key
- **WHEN** currentness is classified
- **THEN** the source is classified stale, and regeneration does not start without the applicable confirmation

#### Scenario: Changed configuration alone is stale

- **GIVEN** an active run whose consumed data is unchanged while the currently-resolved stage configuration differs from the configuration that produced it
- **WHEN** currentness is classified
- **THEN** the source is classified stale, and the run is not reused through applicability

### Requirement: Provenance is carried by a semantic derivation key and explicit pointers

A derived run's `derivation_key` SHALL be an opaque, fixed-length fingerprint of the portable data the stage consumes and the configuration the stage owns, and SHALL NOT embed any database-local identifier.
An upstream run's `derivation_key` SHALL be a key input only when the stage consumes that key as semantic data; it is not a mandatory propagation token, and a new upstream run that yields byte-equivalent consumed data SHALL leave the downstream key unchanged.
Consumers SHALL compare derivation keys only for equality; production code SHALL NOT decode or parse a stored key.
The exact upstream runs or rows a derivation consumed SHALL be recorded as explicit provenance pointers, distinct from both the `derivation_key` and the row identity.
A derived row SHALL be traceable back to its `source_id` one stage at a time through those pointers.

#### Scenario: The derivation key excludes database-local identifiers

- **GIVEN** the same logical derivation performed against two databases whose local row and run ids differ
- **WHEN** the two `derivation_key`s are compared
- **THEN** they are equal, because the key is a function of consumed data and stage-owned configuration only

#### Scenario: An equivalent upstream regeneration leaves the downstream key unchanged

- **GIVEN** a downstream run and a superseding upstream run whose consumed data is byte-equivalent to its predecessor's
- **WHEN** the downstream key for the current inputs is computed
- **THEN** it equals the active run's key, and the upstream run-key change alone changes nothing downstream

#### Scenario: Changed consumed data changes the key

- **GIVEN** a stage whose consumed data changes in any byte
- **WHEN** the derivation key is computed
- **THEN** it differs from the active run's key

#### Scenario: Changed stage-owned configuration changes the key

- **GIVEN** a stage whose owned configuration (producer version, prompt, policy) changes
- **WHEN** the derivation key is computed
- **THEN** it differs from the active run's key

#### Scenario: Configuration is read from version stamps, not the key

- **GIVEN** a stored run whose producing configuration must be inspected
- **WHEN** production code resolves it
- **THEN** it reads version stamps and provenance pointers, and does not decode the derivation key

#### Scenario: A derived row traces back to its source identity

- **GIVEN** a persisted derived row
- **WHEN** its provenance pointers are followed
- **THEN** they resolve, one stage at a time, to the upstream rows it consumed and ultimately to the `source_id` the chain belongs to

### Requirement: Invalidation is lazy by default and gates large reprocessing behind explicit confirmation

A **generation** is the cohort of derived outputs belonging to one active run for a `(stage, scope_id)`.
A change to a producer's version SHALL mark prior generations logically stale without eagerly recomputing them; a stale-but-active generation SHALL remain usable until it is recomputed.
Recompute SHALL occur lazily on access or through an explicit operation.
Any user-initiated reprocessing whose downstream blast radius is large — a corpus-wide backfill, or a base-document edit that cascades through the derivation graph — SHALL require an explicit human confirmation before it runs; the confirmation SHALL warn and require approval, and is not required to compute a precise cost.
Each derived row SHALL record the producer version that produced it, so a version-heterogeneous corpus is valid and the coverage of any version is queryable.

#### Scenario: A version bump does not eagerly recompute

- **GIVEN** an active generation produced under a prior producer version
- **WHEN** the producer version is bumped
- **THEN** the prior generation is marked logically stale but remains active and usable, and no recomputation is triggered until the work is accessed or explicitly requested

#### Scenario: A large-blast-radius reprocessing requires explicit confirmation

- **GIVEN** a user-initiated operation that would reprocess a corpus-wide backfill or cascade a base-document edit through the derivation graph
- **WHEN** the operation is requested
- **THEN** it does not run until an explicit human confirmation is given, after a warning that approval is required

#### Scenario: A version-heterogeneous corpus is valid and queryable

- **GIVEN** a corpus whose derived rows were produced under more than one producer version
- **WHEN** the rows are inspected
- **THEN** each records the version that produced it, the corpus is valid in that mixed state, and the set of rows on any given version is queryable
