# Delta for pipeline-identity

## ADDED Requirements

### Requirement: The durable source identity has one canonical name

The durable source identity SHALL be named `source_id` wherever it appears as a contract term in the data model, public interfaces, and provenance.
A reusable engine or infrastructure layer MAY refer to it through a role-generic alias (for example a run primitive's `scope_id`) only when that layer is genuinely stage-agnostic AND the alias's mapping to `source_id` is documented at the boundary.
Introducing any new identifier or provenance-pointer name SHALL state the role it plays, why an existing name does not fit, and how it resolves back to `source_id`.

Serves: coherent-pipeline-foundation

#### Scenario: The source identity is named source_id where it is a contract term

- **GIVEN** a stage that names the durable source identity in its persisted data model or its public interface
- **WHEN** that name is inspected
- **THEN** it is `source_id`, not a stage-specific synonym

#### Scenario: An engine alias is permitted with a documented boundary mapping

- **GIVEN** a stage-agnostic engine layer that scopes its records by a role-generic key rather than by `source_id` directly
- **WHEN** that alias is inspected
- **THEN** its mapping to `source_id` is documented at the boundary, and the alias is used only within that engine layer

### Requirement: Identifier names signal identity versus fingerprint by suffix

An identifier that names an identity — a stable, pointable handle to a row, entity, or scope — SHOULD use the `_id` suffix; a computed matching fingerprint SHOULD use the `_key` suffix and a content fingerprint the `_hash` suffix, so that a name communicates whether its value is a pointable identity or a derived fingerprint.
A run's scope reference is an identity and is therefore named `scope_id`, not `scope_key`.

Serves: coherent-pipeline-foundation

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

Serves: portable-knowledge, coherent-pipeline-foundation

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

### Requirement: Content fingerprints are observable columns, never identity

A producer that needs change-detection or content provenance SHALL expose the content fingerprint (for example a `content_hash`) as a separately observable column, distinct from the row's identity.
Whether a derived input changed SHALL be determinable by comparing that observable column, not by comparing or recomputing identities.

Serves: coherent-pipeline-foundation, portable-knowledge

#### Scenario: A content change is observable without the identity carrying it

- **GIVEN** the same logical row derived under two generations, once from changed content and once from unchanged content
- **WHEN** the two generations' rows are compared
- **THEN** the content change is visible by comparing the observable content-fingerprint column, while the row's identity remains a stable surrogate that does not itself encode the content

### Requirement: Idempotency is a run-level data-model invariant

A stage's derived outputs SHALL belong to a run for which at most one run per `(stage, scope_id)` is active, recorded and superseded atomically (the run primitive's invariant this capability builds on).
Re-invoking a stage for a scope whose `derivation_key` matches the active run SHALL reuse that run and produce no new run and no duplicate outputs.
Where a producer yields a stable sameness-key for its rows, the data store SHALL reject a duplicate row for that key; where a producer is stochastic and yields no stable sameness-key, row-level non-duplication SHALL rest on run-level reuse together with atomic per-unit writes.

Serves: idempotent-duplicate-free-pipeline

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

### Requirement: Provenance is carried by a semantic derivation key and explicit pointers

A derived run's `derivation_key` SHALL be a fingerprint of its semantic inputs — content fingerprints, producer and configuration versions, and the **upstream** runs' `derivation_key`s — and SHALL NOT embed any database-local identifier.
The exact upstream runs or rows a derivation consumed SHALL be recorded as explicit provenance pointers, distinct from both the `derivation_key` and the row identity.
A derived row SHALL be traceable back to its `source_id` one stage at a time through those pointers.

Serves: portable-knowledge, coherent-pipeline-foundation, idempotent-duplicate-free-pipeline

#### Scenario: The derivation key excludes database-local identifiers

- **GIVEN** the same logical derivation performed against two databases whose local row and run ids differ
- **WHEN** the two `derivation_key`s are compared
- **THEN** they are equal, because the key is a function of semantic inputs and upstream `derivation_key`s only

#### Scenario: An upstream input change propagates to the downstream key

- **GIVEN** a derived run whose `derivation_key` embeds the `derivation_key` of the upstream run it consumed
- **WHEN** the upstream input changes so the upstream run's `derivation_key` changes
- **THEN** the downstream run's `derivation_key` changes as well, marking it for a new generation

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

Serves: affordable-pipeline-evolution

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
