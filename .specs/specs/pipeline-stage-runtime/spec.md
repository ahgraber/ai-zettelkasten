# Pipeline Stage Runtime Specification

> Synced from change `output-sensitive-identity` on 2026-09-06

## Purpose

The pipeline-stage runtime is a primitives package (`aizk.pipeline`) — not a framework — for running queued work in any processing stage.
It provides a claim/drain/cancel/timeout runner that drives discovery, claim, status transition, and cleanup through a stage-supplied **handler protocol**; a separable run/dataset-version primitive keyed by `(stage, scope_id)`; and a shared, append-only transition-event log written through a same-transaction co-commit helper.
Each stage owns its own work-unit tables and identities — the runtime never requires a shared work-unit table or knowledge of any stage's schema.
The current runner is one orchestration-engine implementation; the handler protocol keeps the domain core (execute, classify, stage-owned state writes) narrow enough to survive an engine swap.
Conversion is the first adapter; the graph stages (contextualization, mention extraction) are the next consumers.

## Requirements

### Requirement: The runner operates over a stage-supplied handler protocol

The runtime SHALL drive work-unit processing — discovery, claim, status transition, and cleanup — through a handler protocol; each stage keeps its own work-unit table, and the runtime SHALL NOT require a shared work-unit table.
The lifecycle operations — claim, stale recovery, terminal transition with retry scheduling, operator retry, operator cancel, and capacity counting — SHALL be provided by the runtime for every stage; a stage SHALL supply only its dependencies, its unit of work, its outcome mapping, and any narrowing it declares.
Adding or changing a stage SHALL NOT require modifying the runtime; registering a stage's adapter through the composition root SHALL make that stage runnable.

#### Scenario: A new stage runs purely by supplying an adapter

- **GIVEN** a stage that supplies its dependencies, its unit of work, and its outcome mapping
- **WHEN** the adapter is registered through the composition root
- **THEN** the runtime processes that stage's work-units without any change to the runtime, without a shared work-unit table, and without lifecycle code written by the stage

#### Scenario: Two stages with different stores share the runner

- **GIVEN** two stages whose work-units live in different stage-owned tables with different identities
- **WHEN** both register adapters
- **THEN** the same runner and the same lifecycle operations drive both, each over its own table

### Requirement: Derived outputs belong to runs invalidated atomically at the run level

A stage's derived outputs SHALL belong to a run identified by `(stage, scope_id)`, where the stage defines its own `scope_id` (for example per-document, per-chunk, or corpus-wide).
At most one run per `(stage, scope_id)` SHALL be active.
A run's outputs SHALL NOT be modified after the run is recorded; superseding SHALL be expressed only as a status transition from active to superseded.
Recording a new run and superseding the prior active run for the same `(stage, scope_id)` SHALL occur atomically, so there is never more than one active run nor a window with none.

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

### Requirement: Every work-unit status change is recorded as a durable event in the same transaction

A work-unit's authoritative status SHALL be its single current-state value, and its creation and every subsequent status change SHALL be co-committed in the same transaction with an append-only event recording it.
A creation event SHALL record the path that created the unit and the acting principal.
A request that resolves to an existing work-unit SHALL NOT write a creation event, since it creates nothing.
Each event SHALL validate against a typed contract for its kind.
A committed creation or status change SHALL NOT exist without its event, and an event SHALL NOT exist without its creation or status change.

#### Scenario: A status transition writes exactly one matching event

- **GIVEN** a work-unit in a given status
- **WHEN** its status transitions
- **THEN** the new status and one transition event recording it are durable, committed together

#### Scenario: A failed transition leaves neither the status change nor the event

- **GIVEN** a status transition whose transaction fails before commit
- **WHEN** the store is inspected
- **THEN** neither the status change nor a transition event for it is present

#### Scenario: Creation is evented with origin and principal

- **GIVEN** work with no existing work-unit, created through any path (intake, admission, backfill)
- **WHEN** the work-unit is created
- **THEN** the unit and one creation event recording the creating path and acting principal are durable, committed together

#### Scenario: Reuse writes no creation event

- **GIVEN** work that already has a work-unit
- **WHEN** a request for the same work resolves to the existing unit
- **THEN** no new creation event exists for that unit

#### Scenario: A failed creation leaves neither the unit nor the event

- **GIVEN** a work-unit creation whose transaction fails before commit
- **WHEN** the store is inspected
- **THEN** neither the work-unit nor a creation event for it is present

### Requirement: Work-units and events carry a cross-stage source identity

Every work-unit and every transition event SHALL carry the identity of the source it derives from, so a source's progress is resolvable across stages by that identity.

#### Scenario: A source's events are resolvable across stages by its identity

- **GIVEN** work-units for one source processed by more than one stage
- **WHEN** events are queried by that source identity
- **THEN** the events from each stage are returned together

### Requirement: Work-units follow a generic lifecycle with classified terminal outcomes

The runtime SHALL define the work-unit lifecycle for every stage: a unit is `queued`, then `running`, then reaches exactly one terminal outcome — `succeeded`, `failed`, `cancelled`, or `timed_out`.
Every stage's work-unit status SHALL be one of these six values, and every surface that exposes a work-unit status — API responses, filters, status counts, and the console — SHALL use these values; no stage SHALL define a status of its own.
A `failed` outcome SHALL be classified as retryable or permanent, and the classification SHALL be derivable from the unit alone: retryable when a retry wait is set, permanent when no retry wait is set and the unit is finished.
Only a retryable failure is claimed again automatically; a permanent failure, a cancelled unit, and a timed-out unit leave the queue until an operator retries them.

#### Scenario: Each work-unit reaches exactly one classified terminal outcome

- **GIVEN** work-units that succeed, fail, are cancelled, and time out respectively
- **WHEN** each finishes
- **THEN** each holds exactly one terminal outcome, and each failed unit is marked retryable or permanent

#### Scenario: Only a retryable failure is claimed again automatically

- **GIVEN** one work-unit `failed` with a retry wait set and one `failed` with no retry wait
- **WHEN** the runtime selects work after the wait elapses
- **THEN** the retryable unit is claimed and the permanent one is not

#### Scenario: Every stage exposes only the shared vocabulary

- **GIVEN** conversion, contextualization, and extraction units in every status
- **WHEN** their statuses are read through each stage's API and through the console
- **THEN** every status read is one of the six values, spelled the same on every surface

#### Scenario: A timed-out unit stays terminal until an operator retries it

- **GIVEN** a unit recorded as `timed_out`
- **WHEN** the runtime selects work repeatedly
- **THEN** the unit is not claimed until an operator retries it

### Requirement: Eligible work-units are processed with bounded concurrency in submission order

The runtime SHALL process eligible work-units and SHALL NOT exceed its configured limit of simultaneously executing units; among eligible units, processing SHALL begin in submission order.
A work-unit is eligible when it is `queued`, or when it is `failed` with a retry wait that is set and has elapsed.
A `failed` unit with no retry wait SHALL NOT be claimed.
Claiming a unit SHALL increment its attempt count; no other transition SHALL change the attempt count.

#### Scenario: Concurrency stays within the limit and eligible units start in order

- **GIVEN** more eligible work-units than the configured concurrency limit
- **WHEN** the runtime processes them
- **THEN** the number executing simultaneously never exceeds the limit, and eligible units begin in submission order

#### Scenario: A unit waiting for its retry delay is not yet eligible

- **GIVEN** a retryable-failed work-unit whose retry-wait has not elapsed
- **WHEN** the runtime selects work
- **THEN** that unit is not started until its retry-wait elapses

#### Scenario: A permanently failed unit is never claimed

- **GIVEN** a `failed` work-unit with no retry wait
- **WHEN** the runtime selects work
- **THEN** that unit is not started

#### Scenario: A claim is the only transition that counts an attempt

- **GIVEN** a `queued` work-unit with attempt count 0
- **WHEN** the runtime claims it, it fails with a retry wait, and the runtime claims it again after the wait
- **THEN** its attempt count is 1 after the first claim and 2 after the second, and the failure changed it by nothing

### Requirement: A shutdown signal drains in-flight work within a bounded timeout

On a termination signal the runtime SHALL stop claiming new work, allow in-flight work-units to finish within a bounded drain timeout, then exit; after exit no work-unit's process SHALL remain running, and each in-flight work-unit SHALL have its terminal outcome recorded.
A work-unit terminated because the drain timeout elapsed SHALL be `failed` with an error code naming the drain and SHALL NOT be `cancelled`; below the stage's attempt cap it carries an immediate retry wait, so the next runtime claims it without operator action, and at the cap it is permanent, as a unit reclaimed by stale recovery at the cap is.
When the terminal outcome cannot be committed (for example, the database is unreachable), the work-unit stays `running` in the database, and stale-unit recovery reclaims it once its stale window has elapsed.

#### Scenario: In-flight work finishes during drain

- **GIVEN** work-units in flight when a termination signal arrives
- **WHEN** they complete within the drain timeout
- **THEN** they reach terminal outcomes and the runtime exits with none left running

#### Scenario: Drain timeout is enforced

- **GIVEN** in-flight work that does not complete within the drain timeout
- **WHEN** the timeout elapses
- **THEN** the runtime stops waiting and exits, leaving no work-unit running

#### Scenario: A unit terminated by the drain timeout is retried by the next runtime

- **GIVEN** a work-unit terminated because the drain timeout elapsed
- **WHEN** the next runtime starts and selects work
- **THEN** the unit was `failed` with a retry wait and an error code naming the drain, and it is claimed once its wait elapses

#### Scenario: A unit whose terminal write fails during drain is recovered by the stale sweep

- **GIVEN** a work-unit whose terminal outcome cannot be committed during drain because the database is unreachable
- **WHEN** the runtime exits and a later runtime runs stale-unit recovery after the unit's stale window has elapsed
- **THEN** the unit is reclaimed as stale work, with no process of the exited runtime still running for it

### Requirement: Cancellation is honored within a bounded interval and skips queued units

A request to cancel a running work-unit SHALL take effect within a bounded interval, unless the stage declares the unit's current part of `running` non-cancellable; then the request SHALL be refused, no state SHALL change, and the unit SHALL continue to its terminal outcome.
A work-unit cancelled while still queued SHALL be skipped rather than executed.

#### Scenario: A running work-unit is cancelled promptly

- **GIVEN** a running work-unit outside any part of `running` its stage declares non-cancellable
- **WHEN** it is cancelled
- **THEN** it stops within the bounded cancellation interval and reaches a cancelled outcome

#### Scenario: A queued work-unit cancelled before execution is not run

- **GIVEN** a queued work-unit
- **WHEN** it is cancelled before execution begins
- **THEN** it is skipped and never executed

#### Scenario: A running unit in a non-cancellable part continues

- **GIVEN** a conversion unit `running` in its upload phase
- **WHEN** it is cancelled
- **THEN** the request is refused, the unit's status is unchanged, and the unit reaches `succeeded` or `failed` on its own

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

### Requirement: Lifecycle behavior is identical across stages

For any two stages configured with the same retry policy and observed under the same clock, the same lifecycle input SHALL produce the same lifecycle result: the same next unit claimed under the same queue order; the same status, retry wait, attempt count, and event kind after a failure of the same retry class at the same attempt; the same status and recorded cause after recovery of an interrupted unit; the same status change and event kind after an operator retry or cancel.
A stage MAY narrow the set of units an operator can cancel; apart from such a declared narrowing, no stage SHALL differ from another in any of these results.

#### Scenario: Claim order is the same

- **GIVEN** a conversion stage and a contextualization stage each holding three eligible units queued in the same order
- **WHEN** each stage claims one unit
- **THEN** each claims its earliest-queued unit and increments that unit's attempt count to 1

#### Scenario: A retryable failure below the attempt cap waits the same way

- **GIVEN** a conversion unit and a contextualization unit that each fail with a retryable error on attempt 1, under the same retry policy
- **WHEN** each failure is recorded
- **THEN** both units are `failed` with the same retry wait, both carry attempt count 1, and both record a failure event of the retryable class

#### Scenario: A retryable failure at the attempt cap becomes permanent the same way

- **GIVEN** a conversion unit and an extraction unit that each fail with a retryable error on the last attempt the retry policy allows
- **WHEN** each failure is recorded
- **THEN** both units are `failed` with no retry wait and a finished time, and neither is claimed again

#### Scenario: A permanent failure is recorded the same way

- **GIVEN** a conversion unit and a contextualization unit that each fail with a permanent error
- **WHEN** each failure is recorded
- **THEN** both units are `failed` with no retry wait and a finished time, and both record a failure event of the permanent class

#### Scenario: An interrupted unit is recovered the same way

- **GIVEN** a conversion unit and an extraction unit each left `running` by an abrupt runtime stop
- **WHEN** stale-unit recovery runs
- **THEN** both units leave `running` with the same status and the same retry wait, and each records a recovery event with the same cause

#### Scenario: An operator retry has the same effect

- **GIVEN** a `failed` conversion unit and a `failed` contextualization unit, each with attempt count 2
- **WHEN** an operator retries each
- **THEN** both are `queued` with no retry wait, both keep attempt count 2, and both record a `requeued` event

#### Scenario: A declared cancel narrowing is the only difference

- **GIVEN** the conversion stage, which declares that a running unit in its upload phase cannot be cancelled, and a graph stage, which declares no narrowing
- **WHEN** an operator cancels a running unit of each stage
- **THEN** the graph unit is cancelled, the conversion unit is cancelled unless it is in the upload phase, and a conversion unit in its convert phase is cancelled exactly as the graph unit is

### Requirement: A stage publishes a static definition

Each stage SHALL publish a definition of itself: its name, the shape of one unit's parameters, the derivation of a unit's key from those parameters, its work-unit record type, its event kinds, and its pending-work derivation when it declares one.
The definition SHALL be resolvable without a worker process, a model client, or a database connection, so operator surfaces and command-line tools can name and describe a stage without starting it.
A unit created through the definition SHALL have the same unit key, the same persisted lifecycle fields, the same creation event, and the same returned identity as a unit created through the stage's own enqueue for the same parameters.

#### Scenario: A definition resolves with no runtime configured

- **GIVEN** no database, model client, or object-storage configuration
- **WHEN** a stage's definition is resolved
- **THEN** its name, parameter shape, key derivation, record type, and event kinds are available

#### Scenario: A unit created through the definition equals a domain-enqueued unit

- **GIVEN** the same unit parameters, submitted once through the stage's definition against an empty stage and once through the stage's own enqueue against a second empty stage
- **WHEN** both units are read back
- **THEN** they have the same unit key, the same status, attempt count, and retry wait, the same creation event kind and origin, and the same returned identity

### Requirement: An operator retry returns a terminal unit to the queue and keeps its attempt history

For any stage, an operator retry of a unit in `failed`, `cancelled`, or `timed_out` SHALL return the unit to `queued`, clear its retry wait and its error fields, leave its attempt count unchanged, and record one `requeued` event carrying the retry reason and the acting principal, co-committed with the status change.
A `failed` unit is retryable by an operator whatever its retry class.
The attempt count SHALL change only when the unit is next claimed, so attempt numbers on the event trail never repeat.
A retry of a unit in `queued`, `running`, or `succeeded` SHALL be refused and SHALL alter nothing.

#### Scenario: Retry of a failed unit of either retry class

- **GIVEN** one unit `failed` with a retry wait set and one unit `failed` with no retry wait
- **WHEN** an operator retries each
- **THEN** each unit is `queued` with no retry wait and no error fields, and each has one `requeued` event naming the reason and the principal

#### Scenario: Retry from cancelled and timed out

- **GIVEN** one unit in `cancelled` and one in `timed_out`
- **WHEN** an operator retries each
- **THEN** each unit is `queued` with no retry wait, and each has one `requeued` event naming the reason and the principal

#### Scenario: Retry of a non-terminal or succeeded unit is refused

- **GIVEN** units in `queued`, `running`, and `succeeded`
- **WHEN** an operator retries each
- **THEN** each retry is refused, no status changes, and no event is recorded

#### Scenario: Attempt numbering continues across a retry

- **GIVEN** a unit that reached `failed` on attempt 2
- **WHEN** an operator retries it and the runtime later claims it
- **THEN** the unit's attempt count is 2 after the retry and 3 after the claim, and the events written after the claim carry attempt 3

#### Scenario: A retried unit at the attempt cap runs exactly once more

- **GIVEN** a unit that is `failed` with no retry wait because its last attempt reached the stage's attempt cap
- **WHEN** an operator retries it, the runtime claims it, and that attempt fails with a retryable error
- **THEN** the unit ran once after the retry, and it is `failed` with no retry wait again

### Requirement: An operator cancel applies to queued, running, and failed units

For any stage, an operator cancel of a unit in `queued`, `running`, or `failed` SHALL move the unit to `cancelled`, clear any retry wait, keep the unit's error fields, and record one cancel event co-committed with the status change; a cancelled unit SHALL NOT be claimed again until an operator retries it.
A `failed` unit is cancellable whatever its retry class.
A stage MAY declare a part of `running` during which a unit cannot be cancelled.
A cancel of a unit in such a part, or of a unit in `succeeded`, `cancelled`, or `timed_out`, SHALL be refused and SHALL alter nothing.

#### Scenario: A failed unit awaiting retry is cancelled and does not retry

- **GIVEN** a `failed` unit whose retry wait has not elapsed
- **WHEN** an operator cancels it
- **THEN** the unit is `cancelled` with no retry wait, its error fields are unchanged, a cancel event is recorded, and the runtime never claims it

#### Scenario: A permanently failed unit is cancelled

- **GIVEN** a `failed` unit with no retry wait
- **WHEN** an operator cancels it
- **THEN** the unit is `cancelled`, its error fields are unchanged, and a cancel event is recorded

#### Scenario: Cancel of a terminal unit is refused

- **GIVEN** units in `succeeded`, `cancelled`, and `timed_out`
- **WHEN** an operator cancels each
- **THEN** each cancel is refused, no status changes, and no event is recorded

#### Scenario: A stage-declared part of running is not cancellable

- **GIVEN** a conversion unit that is `running` in its upload phase
- **WHEN** an operator cancels it
- **THEN** the cancel is refused and the unit continues to completion
