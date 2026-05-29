# Delta for conversion-worker

This change extracts conversion's proven worker machinery into the generic `pipeline-stage-runtime` capability (the runner, the run primitive, the shared transition-event log, and the `record_transition` co-commit helper).
The generic contracts below are now owned by that capability; this delta relocates them out of `conversion-worker`, leaving only conversion-specific behavior — event kinds and payloads, the services conversion probes, and the domain processing it owns.

The new home for every relocated requirement is the `pipeline-stage-runtime` capability spec.

## REMOVED Requirements

### Requirement: Process jobs with bounded concurrency in FIFO order

> Previously: "The system SHALL process conversion jobs in first-in-first-out
> order by queue time, with the number of concurrently processing jobs bounded
> by a configurable limit. Job claiming SHALL be atomic — the same job SHALL NOT
> be claimed and processed by two workers concurrently."
> Bounded-concurrency processing in submission order (and the atomic claim that
> backs it) is now a generic runner obligation; it lives in
> `pipeline-stage-runtime` under "Eligible work-units are processed with bounded
> concurrency in submission order." Conversion declares its concurrency limit
> through its stage adapter and supplies the stage-specific eligibility/claim
> query the runner runs.

### Requirement: Stale-job recovery records the recovery cause in the event payload

> Previously: "The system SHALL record each stale-RUNNING-job recovery as a
> status-transition event whose payload identifies the recovery cause: the
> staleness threshold applied and the original start-time of the stale attempt.
> The event kind for stale recovery SHALL be distinct from the kind used for
> ordinary failure transitions ..."
> Stale-work recovery with a recorded cause is now a generic runner obligation;
> it lives in `pipeline-stage-runtime` under "Stale work-units are recovered
> with a recorded cause." The specific recovery payload fields (staleness
> threshold, prior start-time) and the distinct conversion event kind remain
> conversion's choices, expressed through the conversion event kinds and
> payloads retained under "Record every ConversionJob status transition as a
> durable event."

### Requirement: Emit structured logs with trace context

> Previously: "The system SHALL log key processing events with job identifier,
> bookmark identifier, KaraKeep identifier, and status in every log entry to
> enable trace reconstruction."
> Emitting structured lifecycle logs with trace context is now a generic runner
> obligation; it lives in `pipeline-stage-runtime` under "The runtime emits
> lifecycle observability and identifies its stage role."

### Requirement: Emit operational metrics

> Previously: "The system SHALL emit metrics for queue depth, job duration, job
> status counts, fetch latency, and S3 upload latency."
> Emitting operational metrics across the work-unit lifecycle is now a generic
> runner obligation; it lives in `pipeline-stage-runtime` under "The runtime
> emits lifecycle observability and identifies its stage role."

### Requirement: Identify process role for operator monitoring

> Previously: "Every Python process (API server, worker, CLI) SHALL expose its
> role as a human-readable label to enable operators to distinguish process
> types during monitoring."
> Advertising the stage/process role for operator monitoring is now a generic
> runner obligation; it lives in `pipeline-stage-runtime` under "The runtime
> emits lifecycle observability and identifies its stage role."

## MODIFIED Requirements

### Requirement: Transition job status atomically

The system SHALL update job status to `SUCCEEDED`, `FAILED_RETRYABLE`, or `FAILED_PERM` only after the associated S3 or error state is confirmed.
The atomicity of the status update with its transition event is provided by the generic co-commit helper; conversion retains the constraint that a conversion status is set only after the associated S3/error state is confirmed, and that the conversion outcome statuses are `SUCCEEDED`, `FAILED_RETRYABLE`, and `FAILED_PERM`.

> Previously: "... SHALL ensure that the status update and a corresponding event
> record become observable to readers atomically: a committed status update
> SHALL always coincide with a committed event record, and a rollback SHALL
> discard both." The generic same-transaction status+event co-commit is now
> runner-owned (see `pipeline-stage-runtime`, "Every work-unit status change is
> recorded as a durable event in the same transaction"); conversion keeps the
> confirm-before-transition rule and its specific outcome statuses.

#### Scenario: Status set to SUCCEEDED after verified upload

- **GIVEN** all S3 uploads are verified
- **WHEN** the transition commits
- **THEN** the job status is `SUCCEEDED`, a succeeded event record is co-committed, and the output record is visible to consumers

#### Scenario: Retryable error sets status to FAILED_RETRYABLE

- **GIVEN** a transient error occurs during fetch, conversion, or upload
- **WHEN** the error handler runs
- **THEN** the job status transitions to `FAILED_RETRYABLE`, a failure event record carrying the sanitized error identifiers is co-committed, and the prior attempt's event records remain observable

#### Scenario: Permanent error sets status to FAILED_PERM

- **GIVEN** a non-recoverable error (missing content, empty output, egress-policy rejection)
- **WHEN** the error handler runs
- **THEN** the job status transitions to `FAILED_PERM`, a failure event record carrying the sanitized error identifiers and the non-retryable indicator is co-committed, and the full attempt history remains observable

### Requirement: Record every ConversionJob status transition as a durable event

The system SHALL durably record every change to `ConversionJob.status` as a
transition event co-committed with the status change (the same-transaction
co-commit guarantee is provided by the generic helper).

Each event record SHALL identify the affected job, the underlying Source identity, the attempt number that the transition belongs to, the time of the transition, the event kind from a closed enumeration of conversion transition causes, the prior status (absent for the job's first observable state), the new status, and a typed structured cause appropriate to the kind.
The Source identity SHALL be recorded alongside the job identity so that audit queries spanning future processing-stage event records (chunking, embedding, etc.) can be served by Source identity without joining through `ConversionJob`.

Event records SHALL NOT be updated or deleted by the conversion system after insertion; corrections SHALL be expressed as new event records.
The `ConversionJob.status` field SHALL remain the authoritative current-state projection used by scheduling, claiming, and read paths; the event record set SHALL NOT be the source of truth for those paths.

> Previously: this requirement also defined the generic same-transaction
> co-commit invariant ("a committed status change SHALL always coincide with a
> committed event record, and a rolled-back status change SHALL leave no event
> record"). That invariant is now runner-owned (see `pipeline-stage-runtime`,
> "Every work-unit status change is recorded as a durable event in the same
> transaction" and "Work-units and events carry a cross-stage source
> identity"); conversion keeps its closed enumeration of conversion event kinds,
> its per-event payload fields, the attempt/prior-status/new-status semantics,
> the append-only correction rule, and the projection-is-authoritative rule.

#### Scenario: Status transition produces exactly one event atomic with the projection

- **GIVEN** a ConversionJob currently in `QUEUED`
- **WHEN** the worker claims the job and transitions it to `RUNNING`
- **THEN** the same commit that mutates `ConversionJob.status` to `RUNNING` also persists exactly one event record with prior status `QUEUED`, new status `RUNNING`, the attempt number that the new RUNNING state belongs to, and a typed cause payload identifying the claim

#### Scenario: Transition rollback leaves job in prior status with no event record

- **GIVEN** a transition whose commit fails (e.g., constraint violation, database error, or any other exception raised between mutation and commit)
- **WHEN** the surrounding transaction rolls back
- **THEN** subsequent readers observe the job in its prior status, and no event record for the failed transition is observable

#### Scenario: Retry preserves prior attempt's event records

- **GIVEN** a ConversionJob whose first attempt transitioned through `RUNNING → FAILED_RETRYABLE` with one or more recorded event records carrying the attempt-1 attempt number
- **WHEN** the job is reclaimed for a second attempt and subsequently succeeds
- **THEN** the event records for attempt 1 remain observable with the attempt-1 number, and the event records for attempt 2 are observable with the attempt-2 number, with no overwrite or deletion of prior records

#### Scenario: Initial submission event has no prior status

- **GIVEN** a new ConversionJob submitted via the API and persisted with status `QUEUED`
- **WHEN** the corresponding event record is written
- **THEN** the event record carries no prior status, new status `QUEUED`, and the kind that identifies an initial submission

#### Scenario: Permanent failure transition is recorded

- **GIVEN** a job in status `RUNNING` whose subprocess produces a non-retryable failure
- **WHEN** the orchestrator transitions the job to `FAILED_PERM`
- **THEN** an event record is committed with prior status `RUNNING`, new status `FAILED_PERM`, and a typed cause payload carrying the non-retryable indicator and the sanitized error identifiers

#### Scenario: Upload-pending transition is recorded

- **GIVEN** a job whose subprocess has completed conversion successfully
- **WHEN** the orchestrator transitions the job to `UPLOAD_PENDING` prior to invoking upload
- **THEN** an event record is committed with prior status `RUNNING`, new status `UPLOAD_PENDING`, and a typed cause payload identifying the artifact that is about to upload

#### Scenario: Cancellation mid-conversion is recorded

- **GIVEN** a job in status `RUNNING` whose cancellation is requested via the API
- **WHEN** the cancellation transition commits the job to `CANCELLED`
- **THEN** an event record is committed with prior status `RUNNING`, new status `CANCELLED`, and a typed cause payload carrying the cancellation context

#### Scenario: Event records are append-only

- **GIVEN** any persisted event record
- **WHEN** the worker, API, or any other component later processes the same job
- **THEN** no code path mutates or deletes the existing record; new information is expressed by appending additional event records

### Requirement: Event payloads validate against typed per-kind contracts

The system SHALL define, for each kind in the closed enumeration of conversion event kinds, a typed payload contract specifying that kind's permitted fields and field types.
The generic obligation to validate every event payload against its kind's contract before insertion is provided by the runner; this requirement retains the conversion-specific per-kind payload contracts and conversion's forward-compatibility rule.

An unknown kind SHALL fail validation.
A payload whose fields do not satisfy the contract for its kind SHALL fail validation.
Readers SHALL tolerate the future addition of unrecognized fields to a known kind's payload (forward-compatibility with additive changes); incompatible payload changes (renamed fields, removed fields, changed types) SHALL be expressed by introducing a new kind to the closed enumeration rather than by mutating an existing kind's contract.

> Previously: "The system SHALL validate every event-record payload against a
> typed contract specific to the event's kind before insertion. ... the system
> SHALL reject at write time any payload that fails validation, with a typed
> error rather than persisting opaque or unrecognized data." The generic
> validate-typed-payload-before-write obligation is now runner-owned (see
> `pipeline-stage-runtime`, "Every work-unit status change is recorded as a
> durable event in the same transaction" — each event validates against a typed
> contract for its kind); conversion keeps the per-kind payload contracts for
> its own closed enumeration of event kinds and its additive
> forward-compatibility rule.

#### Scenario: Recognized kind with valid fields validates

- **GIVEN** an event with a recognized kind and a payload whose fields satisfy that kind's contract
- **WHEN** the payload is validated before insertion
- **THEN** validation succeeds and the event record is inserted

#### Scenario: Unknown kind rejected

- **GIVEN** an attempt to record an event with a kind that is not a member of the closed enumeration
- **WHEN** validation runs
- **THEN** a typed validation error is raised and no event record is inserted

#### Scenario: Extra field for a known kind rejected on write

- **GIVEN** an attempt to record an event whose payload includes a field not defined on its kind's contract
- **WHEN** validation runs
- **THEN** a typed validation error is raised and no event record is inserted

#### Scenario: Reader tolerates additive field on a previously-persisted record

- **GIVEN** a previously persisted event record whose payload was written under an older code version
- **WHEN** the current reader deserializes the payload
- **THEN** any unrecognized fields are ignored without raising, and the recognized fields are returned

### Requirement: Validate required external services on startup

The system SHALL declare conversion's required external-service probes — S3, the database, and (when configured) the picture-description endpoint — through its registered adapters so the runner's startup-validation gate executes them before any conversion work is accepted.
The set of probes SHALL be determined by the registered adapters and their configuration.

> Previously: "The system SHALL probe required external services at process
> startup ..." The generic startup dependency-validation gate (validate required
> dependencies before accepting work) is now runner-owned (see
> `pipeline-stage-runtime`, "Required dependencies are validated before work is
> accepted"); conversion keeps only which specific services its adapters declare
> as probes and that the probe set is adapter-determined.

#### Scenario: Adapter-declared probe executed at startup

- **GIVEN** the `DoclingConverter` adapter declares a probe for the picture-description endpoint
- **WHEN** the worker starts
- **THEN** the probe is executed alongside S3 and database probes

#### Scenario: Unused adapter probe skipped

- **GIVEN** no KaraKeep fetcher is registered (e.g., in a deployment using only URL-based ingestion)
- **WHEN** the worker starts
- **THEN** no KaraKeep API probe is attempted
