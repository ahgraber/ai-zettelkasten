# Delta for pipeline-stage-runtime

## MODIFIED Requirements

### Requirement: Derived outputs belong to runs invalidated atomically at the run level

A stage's derived outputs SHALL belong to a run identified by `(stage, scope_id)`, where the stage defines its own `scope_id` (for example per-document, per-chunk, or corpus-wide).
At most one run per `(stage, scope_id)` SHALL be active.
A run's outputs SHALL NOT be modified after the run is recorded; superseding SHALL be expressed only as a status transition from active to superseded.
Recording a new run and superseding the prior active run for the same `(stage, scope_id)` SHALL occur atomically, so there is never more than one active run nor a window with none.

Serves: coherent-pipeline-foundation, idempotent-duplicate-free-pipeline

> Previously: the run's scope reference was named `scope_key`; renamed to `scope_id` under the `_id`-for-identity convention (an identity reference, not a computed fingerprint).

#### Scenario: A new run supersedes the prior one atomically

- **GIVEN** an active run for a `(stage, scope_id)`
- **WHEN** a new run is recorded for the same `(stage, scope_id)`
- **THEN** in one atomic step the new run becomes active and the prior becomes superseded, with the prior run's outputs present and unmodified

#### Scenario: Concurrent attempts to open a new run leave exactly one active

- **GIVEN** two attempts to record a new run for the same `(stage, scope_id)` at once
- **WHEN** both complete
- **THEN** exactly one run is active afterward and no outputs are mutated

#### Scenario: A failed supersession transaction changes nothing

- **GIVEN** an active run and a new-run transaction that fails before commit
- **WHEN** the store is inspected
- **THEN** the prior run is still active and no partial new run is present
