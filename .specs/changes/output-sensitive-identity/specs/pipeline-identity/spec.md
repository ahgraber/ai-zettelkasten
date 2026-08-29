# Delta for pipeline-identity

## ADDED Requirements

### Requirement: Every identifier belongs to exactly one tier of the identifier taxonomy

Every internal persisted identifier SHALL belong to exactly one of five tiers: a **surrogate locator** (a database-local row reference), a **semantic identity** (a stable, pointable handle to a source, row, or scope), a **matching key** (a computed fingerprint used for equality-based reuse or deduplication — idempotency keys, sameness keys, occurrence keys), a **derivation key** (a fingerprint of a stage's consumed inputs), or a **content hash** (a fingerprint of raw content).
An external system's identifier is a provenance attribute value, not an internal identifier, and is outside the taxonomy.
A semantic identity SHALL be UUID-typed wherever it is persisted or exchanged; no identity column is string-typed.
A UUID identity serialized in canonical form inside a declared JSON locator (a documented boundary) is conformant.
A matching key SHALL NOT be used as a pointable identity.
A surrogate locator SHALL NOT appear in a derivation key, a content hash, or any portability claim; it MAY serve as an API resource handle.

Serves: rebuildable-corpus

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

### Requirement: External identifiers are provenance attributes, never internal identity

When a source is imported from an external system that carries its own identifier, the internal `source_id` SHALL be a newly minted identity, and the external identifier SHALL be recorded as a provenance attribute on the source.
The external identifier SHALL remain resolvable for correlation; it SHALL NOT be adopted as, or embedded in, any internal identity.

Serves: rebuildable-corpus

#### Scenario: An imported source mints its own identity

- **GIVEN** a source imported from an external bookmark system carrying that system's identifier
- **WHEN** the source row is inspected
- **THEN** its `source_id` is an internally minted identity, and the external identifier is present as a provenance attribute

#### Scenario: The external identifier stays correlatable

- **GIVEN** a source imported with an external identifier
- **WHEN** the external system's record must be matched to the internal source
- **THEN** the match resolves through the recorded provenance attribute, not through any internal identity

### Requirement: A database rebuild remints identities and correlates through source facts

A **rebuild** deletes the database and reproduces derived state by re-ingesting raw inputs and replaying stages; it is distinct from a migration or restore, which preserves rows.
After a rebuild, no internal identity is required to match its pre-rebuild value.
Correlation of a logical source across a rebuild SHALL be achievable through its source metadata and content fingerprints, which SHALL remain queryable.
No consumer outside the system SHALL durably depend on an internal identity surviving a rebuild.

Serves: rebuildable-corpus

#### Scenario: A migration preserves identities

- **GIVEN** a database migrated to another backend or restored from a snapshot
- **WHEN** identities and references are inspected
- **THEN** they resolve unchanged (the existing surrogate contract)

#### Scenario: A rebuild remints identities but preserves correlation

- **GIVEN** the same raw inputs ingested before and after a rebuild
- **WHEN** the two databases are compared
- **THEN** internal identities differ, and each logical source is matchable across the two by its source metadata and content fingerprints

### Requirement: Applicability records extend a run's validity to equivalent later inputs

When an upstream generation changes and the candidate key for the new inputs under the currently-resolved stage configuration equals the active run's `derivation_key`, the stage SHALL record an append-only **applicability** relation from the active run to the new inputs, without superseding the run and without invoking the producer.
An applicability relation SHALL be valid only when its referenced inputs exist, carry the expected role, belong to the run's source scope, and match the recomputed candidate key, and only when the run's outputs are present and complete; an invalid relation SHALL be rejected.
Writing an identical relation twice SHALL be idempotent.
A run referenced by a retained provenance or applicability record SHALL remain resolvable; it is not eligible for compaction while the reference is retained.
Applicability is distinct from provenance: the original provenance pointers remain immutable and continue to identify the exact inputs the producer consumed.

Serves: avoid-redundant-inference

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

Serves: avoid-redundant-inference, trustworthy-operator-view

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

## MODIFIED Requirements

### Requirement: Content fingerprints are observable columns, never identity

> Previously: the requirement covered change-detection fingerprints generally; it did not require a stage to persist a fingerprint of the output downstream stages consume.

A producer that needs change-detection or content provenance SHALL expose the content fingerprint (for example a `content_hash`) as a separately observable column, distinct from the row's identity.
Whether a derived input changed SHALL be determinable by comparing that observable column, not by comparing or recomputing identities.
A stage whose output is consumed by a downstream stage SHALL persist an observable fingerprint of that consumable output at production time, so downstream currentness is classifiable by comparing persisted fingerprints without re-reading or re-hashing content.

Serves: avoid-redundant-inference, trustworthy-operator-view

#### Scenario: A content change is observable without the identity carrying it

- **GIVEN** the same logical row derived under two generations, once from changed content and once from unchanged content
- **WHEN** the two generations' rows are compared
- **THEN** the content change is visible by comparing the observable content-fingerprint column, while the row's identity remains a stable surrogate that does not itself encode the content

#### Scenario: Downstream classification reads persisted fingerprints

- **GIVEN** an upstream generation with a persisted output fingerprint and a downstream run over it
- **WHEN** the downstream source's currentness is classified
- **THEN** the classification compares persisted fingerprint columns and does not load or re-hash the upstream content

### Requirement: Provenance is carried by a semantic derivation key and explicit pointers

> Previously: the derivation key was required to embed the upstream runs' `derivation_key`s, so any upstream key change propagated to the downstream key; stored keys were transparent and production code read fields out of them.

A derived run's `derivation_key` SHALL be an opaque, fixed-length fingerprint of the portable data the stage consumes and the configuration the stage owns, and SHALL NOT embed any database-local identifier.
An upstream run's `derivation_key` SHALL be a key input only when the stage consumes that key as semantic data; it is not a mandatory propagation token, and a new upstream run that yields byte-equivalent consumed data SHALL leave the downstream key unchanged.
Consumers SHALL compare derivation keys only for equality; production code SHALL NOT decode or parse a stored key.
The exact upstream runs or rows a derivation consumed SHALL be recorded as explicit provenance pointers, distinct from both the `derivation_key` and the row identity.
A derived row SHALL be traceable back to its `source_id` one stage at a time through those pointers.

Serves: avoid-redundant-inference, rebuildable-corpus

<!-- modified-removes: An upstream input change propagates to the downstream key -->

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
