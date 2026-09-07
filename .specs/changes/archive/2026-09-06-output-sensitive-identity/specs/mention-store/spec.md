# Delta for mention-store

## ADDED Requirements

### Requirement: An extraction run records its exact upstream input as a stage-owned record

Every extraction run SHALL persist, in the same transaction that records the run, an immutable input record identifying the exact upstream run it consumed, the input policy in force, and the fingerprint of the ordered input payload it read.
The record is provenance, distinct from the run's derivation key and from any applicability relation; it SHALL never be mutated, including when the run is later reused for an equivalent input through applicability.

Serves: avoid-redundant-inference, rebuildable-corpus

#### Scenario: A run's input record is resolvable

- **GIVEN** a persisted extraction run
- **WHEN** its input record is looked up
- **THEN** it identifies the exact upstream run consumed and the input policy, and both resolve

#### Scenario: Reuse leaves the input record unchanged

- **GIVEN** an extraction run reused for a newer equivalent upstream via an applicability relation
- **WHEN** its input record is inspected
- **THEN** it still identifies the originally consumed upstream run, unchanged
