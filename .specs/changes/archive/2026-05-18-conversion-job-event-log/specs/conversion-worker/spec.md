# Delta for conversion-worker

## ADDED Requirements

### Requirement: Record every ConversionJob status transition as a durable event

The system SHALL durably record every change to `ConversionJob.status` such that the projection (the `ConversionJob` current-state record) and the durable event record become observable to readers atomically: a committed status change SHALL always coincide with a committed event record, and a rolled-back status change SHALL leave no event record.

Each event record SHALL identify the affected job, the underlying Source identity, the attempt number that the transition belongs to, the time of the transition, the event kind from a closed enumeration of transition causes, the prior status (absent for the job's first observable state), the new status, and a typed structured cause appropriate to the kind.
The Source identity SHALL be recorded alongside the job identity so that audit queries spanning future processing-stage event records (chunking, embedding, etc.) can be served by Source identity without joining through `ConversionJob`.

Event records SHALL NOT be updated or deleted by the conversion system after insertion; corrections SHALL be expressed as new event records.
The `ConversionJob.status` field SHALL remain the authoritative current-state projection used by scheduling, claiming, and read paths; the event record set SHALL NOT be the source of truth for those paths.

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

### Requirement: Persist subprocess-reported phase events to the event log

The system SHALL persist every `phase` event that the conversion subprocess reports to the parent process as an event record scoped to the job and attempt under which the subprocess is running.
A `phase` event reports progress within the `RUNNING` state and SHALL produce an event record without altering `ConversionJob.status`.
A failure to persist a phase event record SHALL be logged and SHALL NOT halt processing of the job; the subprocess channel is the authoritative real-time signal, and the event record set is a durable replay record.

Subprocess-reported terminal events (`completed`, `failed`, `cancelled`) SHALL drive the parent's control flow but SHALL NOT produce their own event records.
For each subprocess-driven terminal outcome, the canonical event record is the transition event committed by the parent under the "Record every ConversionJob status transition" requirement, after the parent applies all error-detail sanitization rules that constrain durable storage (including the prohibition on persisting rejected destinations from egress-policy failures).

Subprocess-reported phase event payloads SHALL be validated against the closed contract for the `phase` kind before persistence; phase reports whose payload fails validation SHALL be discarded with a log entry and SHALL NOT produce an event record.

#### Scenario: Phase event recorded without status change

- **GIVEN** a job in status `RUNNING` whose subprocess reports a recognized phase
- **WHEN** the parent drains the report
- **THEN** one event record is committed identifying the phase and the current attempt, and `ConversionJob.status` is unchanged

#### Scenario: Multiple phases for the same attempt each produce a record

- **GIVEN** a subprocess that reports two distinct phases during a single attempt
- **WHEN** the parent drains both reports
- **THEN** two distinct event records exist for that job and attempt, one per reported phase, in the order reported

#### Scenario: Subprocess-reported terminal event does not produce its own record

- **GIVEN** a subprocess that reports `failed` with a retryable error code, followed by the orchestrator transitioning the job to `FAILED_RETRYABLE`
- **WHEN** the parent drains the subprocess report and the orchestrator commits the transition
- **THEN** the only failure-related event record observable for that attempt is the orchestrator-committed transition event carrying the sanitized error identifiers

#### Scenario: Subprocess-reported cancellation does not produce its own record

- **GIVEN** a job whose API-side cancellation transition has already committed and whose subprocess subsequently reports `cancelled`
- **WHEN** the parent drains the subprocess report
- **THEN** no additional cancellation event record is appended; the API-side transition event is the sole cancellation record

#### Scenario: Egress-policy rejection detail not persisted via subprocess channel

- **GIVEN** a subprocess that reports `failed` with an egress-policy error code and a traceback containing a rejected destination
- **WHEN** the parent drains the report and the orchestrator commits the resulting transition
- **THEN** no event record observable to durable readers carries the rejected destination; the orchestrator's transition event carries only the sanitized error code

#### Scenario: Phase-event persistence failure does not halt the job

- **GIVEN** a subprocess that reports a recognized phase successfully and an event-record persistence failure during that write
- **WHEN** the parent attempts to record the phase event
- **THEN** the failure is logged with the job and attempt identifiers, the job continues processing, and the next reported phase still attempts its own persistence

#### Scenario: Phase event with unrecognized payload is dropped, not persisted

- **GIVEN** a subprocess that reports a phase whose payload fields do not match the closed contract for the `phase` kind (e.g., an unrecognized phase name or an extra unspecified field)
- **WHEN** the parent attempts to record the phase event
- **THEN** the report is logged as a validation failure and no event record is appended; the job continues processing

### Requirement: Record Source enrichment writes as events scoped to the originating job

The system SHALL record every attempted Source-row enrichment write performed by the conversion worker as an event record, identifying the originating job, the targeted Source identity, the set of mutable Source columns whose values the worker attempted to write, and whether the underlying Source UPDATE succeeded.
The event record SHALL be written whether the Source UPDATE succeeded or failed; the audit record is independent of the best-effort write semantics of Source enrichment.
The event record SHALL NOT be written for Source-row mutations that the conversion worker does not author.

#### Scenario: Successful enrichment records event with success indicator

- **GIVEN** a job whose subprocess produced source metadata and a Source UPDATE that commits successfully
- **WHEN** the worker enriches the Source row
- **THEN** an enrichment event record is appended scoped to the originating job, identifying the Source by its durable identity, the columns the worker wrote, and an indicator that the UPDATE succeeded

#### Scenario: Failed enrichment still records event with failure indicator

- **GIVEN** a job whose Source UPDATE fails after the worker attempts enrichment
- **WHEN** the failure is caught
- **THEN** an enrichment event record is still appended scoped to the originating job, identifying the columns the worker attempted to write and an indicator that the UPDATE failed, and the conversion job proceeds to completion as before

#### Scenario: No event for Source rows the worker did not author

- **GIVEN** a Source row whose mutable columns are written by code outside the conversion worker
- **WHEN** the change is committed
- **THEN** no enrichment event record is appended; the requirement applies only to writes the worker itself authors

### Requirement: Stale-job recovery records the recovery cause in the event payload

The system SHALL record each stale-RUNNING-job recovery as a status-transition event whose payload identifies the recovery cause: the staleness threshold applied and the original start-time of the stale attempt.
The event kind for stale recovery SHALL be distinct from the kind used for ordinary failure transitions so that operators can separate recovered attempts from subprocess-reported failures when querying history.

#### Scenario: Recovered stale job carries staleness payload

- **GIVEN** a job in status `RUNNING` whose start-time is older than the worker's configured stale-job threshold
- **WHEN** the worker's stale-job sweep transitions the job to `FAILED_RETRYABLE`
- **THEN** a single event record is appended with prior status `RUNNING`, new status `FAILED_RETRYABLE`, the stale-recovery kind, and a payload identifying the threshold and the prior start-time

#### Scenario: Ordinary retryable failure does not use the recovery kind

- **GIVEN** a job whose subprocess reports a retryable failure
- **WHEN** the orchestrator's error handler transitions the job to `FAILED_RETRYABLE`
- **THEN** the resulting transition event carries the ordinary-failure kind, not the stale-recovery kind, and its payload carries the reported error identifiers rather than a staleness threshold

### Requirement: Event payloads validate against typed per-kind contracts

The system SHALL validate every event-record payload against a typed contract specific to the event's kind before insertion.
Each kind in the closed enumeration of event kinds SHALL have its own contract defining the permitted fields and field types; the system SHALL reject at write time any payload that fails validation, with a typed error rather than persisting opaque or unrecognized data.

An unknown kind SHALL fail validation.
A payload whose fields do not satisfy the contract for its kind SHALL fail validation.
Readers SHALL tolerate the future addition of unrecognized fields to a known kind's payload (forward-compatibility with additive changes); incompatible payload changes (renamed fields, removed fields, changed types) SHALL be expressed by introducing a new kind to the closed enumeration rather than by mutating an existing kind's contract.

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

## MODIFIED Requirements

### Requirement: Transition job status atomically

The system SHALL update job status to `SUCCEEDED`, `FAILED_RETRYABLE`, or `FAILED_PERM` only after the associated S3 or error state is confirmed, and SHALL ensure that the status update and a corresponding event record become observable to readers atomically: a committed status update SHALL always coincide with a committed event record, and a rollback SHALL discard both.
All other constraints from the prior requirement (atomic projection update, status set only after associated state is confirmed) SHALL be preserved. (Previously: the requirement specified atomic projection updates to terminal states but did not require a co-committed event record, so transitions left no per-attempt history; `error_code`, `error_message`, and `last_error_at` on `ConversionJob` were overwritten on each retry.)

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
