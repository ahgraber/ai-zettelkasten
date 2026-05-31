# Pipeline Stage Runtime Specification

> Synced from change `pipeline-stage-runtime` on 2026-05-31

## Purpose

The pipeline-stage runtime is a primitives package (`aizk.pipeline`) — not a framework — for running queued work in any processing stage.
It provides a claim/drain/cancel/timeout runner that drives discovery, claim, status transition, and cleanup through a stage-supplied **handler protocol**; a separable run/dataset-version primitive keyed by `(stage, scope_key)`; and a shared, append-only transition-event log written through a same-transaction co-commit helper.
Each stage owns its own work-unit tables and identities — the runtime never requires a shared work-unit table or knowledge of any stage's schema.
The current runner is one orchestration-engine implementation; the handler protocol keeps the domain core (execute, classify, stage-owned state writes) narrow enough to survive an engine swap.
Conversion is the first adapter; the graph stages (contextualization, mention extraction) are the next consumers.

## Requirements

### Requirement: The runner operates over a stage-supplied handler protocol

The runtime SHALL drive work-unit processing — discovery, claim, status transition, and cleanup — through a handler protocol that each stage implements over its own store; the runtime SHALL NOT require a shared work-unit table or knowledge of any stage's schema.
Adding or changing a stage SHALL NOT require modifying the runtime; registering a stage's adapter through the composition root SHALL make that stage runnable.

#### Scenario: A new stage runs purely by supplying an adapter

- **GIVEN** a stage that implements the handler protocol over its own store and supplies a unit-of-work adapter
- **WHEN** the adapter is registered through the composition root
- **THEN** the runtime processes that stage's work-units without any change to the runtime or any shared work-unit table

#### Scenario: Two stages with different stores share the runner

- **GIVEN** two stages whose work-units live in different stage-owned tables with different identities
- **WHEN** both register adapters
- **THEN** the same runner drives both, each through its own repository implementation

### Requirement: Derived outputs belong to runs invalidated atomically at the run level

A stage's derived outputs SHALL belong to a run identified by `(stage, scope_key)`, where the stage defines its own `scope_key` (for example per-document, per-chunk, or corpus-wide).
At most one run per `(stage, scope_key)` SHALL be active.
A run's outputs SHALL NOT be modified after the run is recorded; superseding SHALL be expressed only as a status transition from active to superseded.
Recording a new run and superseding the prior active run for the same `(stage, scope_key)` SHALL occur atomically, so there is never more than one active run nor a window with none.

#### Scenario: A new run supersedes the prior one atomically

- **GIVEN** an active run for a `(stage, scope_key)`
- **WHEN** a new run is recorded for the same `(stage, scope_key)`
- **THEN** in one atomic step the new run becomes active and the prior becomes superseded, with the prior run's outputs present and unmodified

#### Scenario: Concurrent attempts to open a new run leave exactly one active

- **GIVEN** two attempts to record a new run for the same `(stage, scope_key)` at once
- **WHEN** both complete
- **THEN** exactly one run is active afterward and no outputs are mutated

#### Scenario: A failed supersession transaction changes nothing

- **GIVEN** an active run and a new-run transaction that fails before commit
- **WHEN** the store is inspected
- **THEN** the prior run is still active and no partial new run is present

### Requirement: Every work-unit status change is recorded as a durable event in the same transaction

A work-unit's authoritative status SHALL be its single current-state value, and every status change SHALL be co-committed in the same transaction with an append-only transition event recording it.
Each event SHALL validate against a typed contract for its kind.
A committed status change SHALL NOT exist without its event, and an event SHALL NOT exist without its status change.

#### Scenario: A status transition writes exactly one matching event

- **GIVEN** a work-unit in a given status
- **WHEN** its status transitions
- **THEN** the new status and one transition event recording it are durable, committed together

#### Scenario: A failed transition leaves neither the status change nor the event

- **GIVEN** a status transition whose transaction fails before commit
- **WHEN** the store is inspected
- **THEN** neither the status change nor a transition event for it is present

### Requirement: Work-units and events carry a cross-stage source identity

Every work-unit and every transition event SHALL carry the identity of the source it derives from, so a source's progress is resolvable across stages by that identity.

#### Scenario: A source's events are resolvable across stages by its identity

- **GIVEN** work-units for one source processed by more than one stage
- **WHEN** events are queried by that source identity
- **THEN** the events from each stage are returned together

### Requirement: Work-units follow a generic lifecycle with classified terminal outcomes

The runtime SHALL define a generic work-unit lifecycle: a unit is queued, then running, then reaches exactly one terminal outcome — succeeded, failed, cancelled, or timed out.
A failed outcome SHALL be classified as retryable or permanent.
Stage-specific statuses SHALL map onto this generic lifecycle, so the runner reasons about progress and retry uniformly across stages.

#### Scenario: Each work-unit reaches exactly one classified terminal outcome

- **GIVEN** work-units that succeed, fail, are cancelled, and time out respectively
- **WHEN** each finishes
- **THEN** each holds exactly one terminal outcome, and each failed unit is marked retryable or permanent

#### Scenario: Only retryable terminal failures are eligible for retry

- **GIVEN** one work-unit in a retryable-failed outcome and one in a permanent-failed outcome
- **WHEN** retry eligibility is evaluated
- **THEN** the retryable unit is eligible and the permanent one is not

### Requirement: Eligible work-units are processed with bounded concurrency in submission order

The runtime SHALL process eligible work-units — those queued and past any retry-wait — and SHALL NOT exceed its configured limit of simultaneously executing units; among eligible units, processing SHALL begin in submission order.

#### Scenario: Concurrency stays within the limit and eligible units start in order

- **GIVEN** more eligible work-units than the configured concurrency limit
- **WHEN** the runtime processes them
- **THEN** the number executing simultaneously never exceeds the limit, and eligible units begin in submission order

#### Scenario: A unit waiting for its retry delay is not yet eligible

- **GIVEN** a retryable-failed work-unit whose retry-wait has not elapsed
- **WHEN** the runtime selects work
- **THEN** that unit is not started until its retry-wait elapses

### Requirement: A shutdown signal drains in-flight work within a bounded timeout

On a termination signal the runtime SHALL stop claiming new work, allow in-flight work-units to finish within a bounded drain timeout, then exit; after exit no work-unit SHALL remain running.

#### Scenario: In-flight work finishes during drain

- **GIVEN** work-units in flight when a termination signal arrives
- **WHEN** they complete within the drain timeout
- **THEN** they reach terminal outcomes and the runtime exits with none left running

#### Scenario: Drain timeout is enforced

- **GIVEN** in-flight work that does not complete within the drain timeout
- **WHEN** the timeout elapses
- **THEN** the runtime stops waiting and exits, leaving no work-unit running

### Requirement: Cancellation is honored within a bounded interval and skips queued units

A request to cancel a running work-unit SHALL take effect within a bounded interval; a work-unit cancelled while still queued SHALL be skipped rather than executed.

#### Scenario: A running work-unit is cancelled promptly

- **GIVEN** a running work-unit
- **WHEN** it is cancelled
- **THEN** it stops within the bounded cancellation interval and reaches a cancelled outcome

#### Scenario: A queued work-unit cancelled before execution is not run

- **GIVEN** a queued work-unit
- **WHEN** it is cancelled before execution begins
- **THEN** it is skipped and never executed

### Requirement: Execution is bounded and terminates cleanly on every outcome

A work-unit exceeding its configured wall-clock timeout SHALL be terminated and recorded with a timed-out outcome.
The runtime SHALL release a work-unit's transient resources on every terminal outcome — succeeded, failed, cancelled, or timed out.
For a stage whose unit-of-work runs in an isolated subprocess, termination SHALL attempt graceful before forceful termination and SHALL leave no orphaned descendant processes; for an in-process unit-of-work, termination SHALL cancel the in-process work and release its resources.

#### Scenario: A timed-out work-unit reaches the timed-out outcome

- **GIVEN** a work-unit running past its wall-clock timeout
- **WHEN** the timeout elapses
- **THEN** it is terminated and recorded with a timed-out outcome

#### Scenario: A subprocess-isolated work-unit leaves no orphan descendants

- **GIVEN** a stage that runs its unit-of-work in an isolated subprocess, with a unit terminated by timeout or cancellation
- **WHEN** termination completes
- **THEN** graceful termination was attempted before forceful, and no orphaned descendant processes remain

#### Scenario: Resources are released on each terminal outcome

- **GIVEN** work-units ending in succeeded, failed, cancelled, and timed-out respectively
- **WHEN** each reaches its terminal outcome
- **THEN** the runtime has released that unit's transient resources in every case

### Requirement: Stale work-units are recovered with a recorded cause

A work-unit left running by an interrupted runtime SHALL be recoverable, and recovery SHALL record its cause in the transition event.

#### Scenario: A stranded running work-unit is recovered with cause

- **GIVEN** a work-unit left running after an abrupt runtime stop
- **WHEN** stale-unit recovery runs
- **THEN** the work-unit leaves the running state and a transition event records the recovery cause

### Requirement: Required dependencies are validated before work is accepted

The runtime SHALL validate its required external dependencies at startup and SHALL NOT begin accepting work-units until that validation succeeds.

#### Scenario: A missing dependency blocks work acceptance

- **GIVEN** a required dependency unavailable at startup
- **WHEN** the runtime starts
- **THEN** startup validation fails and no work-unit is accepted for processing

### Requirement: The runtime emits lifecycle observability and identifies its stage role

The runtime SHALL emit structured logs carrying trace context and operational metrics across the work-unit lifecycle, and the process SHALL advertise its stage role for operator monitoring.

#### Scenario: Lifecycle is observable and the process is identifiable

- **GIVEN** a running stage
- **WHEN** work-units are processed
- **THEN** structured lifecycle logs with trace context and operational metrics are emitted, and the process advertises its stage role
