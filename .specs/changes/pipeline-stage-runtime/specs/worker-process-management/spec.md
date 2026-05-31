# Delta for worker-process-management

This change extracts conversion's proven worker machinery into the generic `pipeline-stage-runtime` capability (the runner).
The generic process/runtime mechanics below are now owned by that capability; this delta relocates them out of `worker-process-management`, leaving only the conversion-specific subprocess behavior that the runner cannot generalize.

The new home for every relocated requirement is the `pipeline-stage-runtime` capability spec.

## REMOVED Requirements

### Requirement: Leave no descendant processes behind when stopping a job

> Previously: "When stopping a job, the system SHALL ensure that no processes
> spawned — directly or transitively — by the conversion phase remain running."
> The no-orphaned-descendant guarantee for subprocess-isolated work is now a
> generic runner obligation; it lives in `pipeline-stage-runtime` under
> "Execution is bounded and terminates cleanly on every outcome" (the
> subprocess-isolated termination clause).

### Requirement: Detect and respond to job cancellation within 2 seconds

> Previously: "The system SHALL poll for job cancellation in the parent process
> and terminate the conversion subprocess when a cancellation is detected, with
> detection latency not exceeding 2 seconds."
> Honoring a cancellation request within a bounded interval is now a generic
> runner obligation; it lives in `pipeline-stage-runtime` under "Cancellation
> is honored within a bounded interval and skips queued units." The conversion
> adapter supplies only the cooperative cancellation hook for its unit-of-work.

### Requirement: Skip processing of cancelled queued jobs

> Previously: "The system SHALL not begin processing a job that is already
> CANCELLED when a worker picks it up."
> Skipping a unit cancelled while still queued is now a generic runner
> obligation; it lives in `pipeline-stage-runtime` under "Cancellation is
> honored within a bounded interval and skips queued units."

### Requirement: Enforce a wall-clock timeout on job execution

> Previously: "The system SHALL terminate a job and mark it retryable-failed if
> it exceeds the configured total execution timeout, covering all phases
> including preflight, conversion, upload, and retry delays."
> Wall-clock timeout enforcement that drives a unit to the timed-out outcome is
> now a generic runner obligation; it lives in `pipeline-stage-runtime` under
> "Execution is bounded and terminates cleanly on every outcome." The conversion
> adapter declares the timeout value; the runner enforces it.

### Requirement: Attempt graceful termination before forceful termination

> Previously: "When stopping a subprocess, the system SHALL first request a
> graceful exit and allow a bounded grace period for the subprocess to exit
> cleanly before escalating to a forceful termination."
> Graceful-before-forceful termination of a subprocess-isolated unit is now a
> generic runner obligation; it lives in `pipeline-stage-runtime` under
> "Execution is bounded and terminates cleanly on every outcome" (the
> subprocess-isolated termination clause).

### Requirement: Handle SIGTERM and SIGINT by draining in-flight work before exiting

> Previously: "The worker process SHALL register signal handlers for SIGTERM and
> SIGINT that initiate a graceful shutdown sequence: stop polling for new jobs,
> allow all in-flight jobs to complete within a bounded drain timeout, and then
> exit."
> Signal-driven graceful drain is now a generic runner obligation; it lives in
> `pipeline-stage-runtime` under "A shutdown signal drains in-flight work within
> a bounded timeout."

### Requirement: Enforce a bounded drain timeout on graceful shutdown

> Previously: "The worker process SHALL enforce a configurable drain timeout
> (default: 300 seconds) after receiving a shutdown signal. If any in-flight
> jobs do not complete within this timeout, the worker SHALL terminate them
> using the existing subprocess termination sequence and then exit."
> The bounded drain timeout is now a generic runner obligation; it lives in
> `pipeline-stage-runtime` under "A shutdown signal drains in-flight work within
> a bounded timeout."

### Requirement: Leave no jobs in RUNNING state after worker exit

> Previously: "The worker process SHALL ensure that no jobs remain in RUNNING
> state when the worker exits, whether the exit is due to a completed drain or a
> drain timeout. Jobs that cannot complete are transitioned to FAILED_RETRYABLE
> so they are eligible for pickup by a restarted worker."
> The guarantee that no work-unit remains running after exit is now a generic
> runner obligation; it lives in `pipeline-stage-runtime` under "A shutdown
> signal drains in-flight work within a bounded timeout" (no work-unit remains
> running after exit). Re-eligibility of stranded units is covered there and by
> "Stale work-units are recovered with a recorded cause."

### Requirement: Log shutdown lifecycle events

> Previously: "The worker process SHALL log structured messages at each stage of
> the shutdown sequence for operational observability."
> Shutdown-lifecycle observability is part of the generic runner lifecycle
> observability; it lives in `pipeline-stage-runtime` under "The runtime emits
> lifecycle observability and identifies its stage role."

## MODIFIED Requirements

### Requirement: Isolate job conversion from the worker process

The conversion stage SHALL run its document-conversion unit-of-work in an
isolated subprocess (the optional subprocess-isolation capability the runner
exposes), declaring subprocess isolation through its stage adapter so that a
crash or hang during conversion does not affect the worker process or any other
in-flight unit, and so that any memory and OS resources acquired during
conversion are fully released on completion.

The generic guarantees that follow from isolation — crash containment across
in-flight units, graceful-before-forceful termination, and no orphaned
descendant processes — are owned by `pipeline-stage-runtime`; this requirement
retains only conversion's choice to opt into subprocess isolation for its
conversion phase and the subprocess model used to achieve it.

> Previously: "The system SHALL isolate each job's document conversion phase
> such that a crash or hang during conversion does not affect the worker process
> or any other in-flight job, and such that any memory and OS resources acquired
> during conversion are fully released on completion." The generic
> optional-subprocess-isolation capability is now runner-owned (see
> `pipeline-stage-runtime`, "Execution is bounded and terminates cleanly on
> every outcome"); conversion keeps only its opt-in to subprocess isolation for
> the conversion phase.

#### Scenario: Conversion crash does not affect other jobs

- **GIVEN** a job's conversion phase crashes or hangs
- **WHEN** the worker observes the failure
- **THEN** that job is marked as retryable-failed and other in-flight jobs continue processing without disruption

### Requirement: Clean up temporary workspace on all job outcomes

The conversion stage's primary transient resource is the temporary workspace created for a job; the conversion adapter SHALL remove that workspace on every terminal outcome — succeeded, failed, cancelled, or timed out — by scoping it to the adapter's unit-of-work execution, so no workspace survives the unit regardless of how it ends.
The generic guarantee that transient resources are released on every terminal outcome is owned by `pipeline-stage-runtime`; this requirement retains only the conversion-specific obligation to remove the temporary workspace.

> Previously: "The system SHALL guarantee that the temporary workspace created
> for a job is removed after the job finishes, regardless of whether it
> succeeded, failed, was cancelled, or raised an exception." The generic
> cleanup-on-every-outcome obligation is now runner-owned (see
> `pipeline-stage-runtime`, "Execution is bounded and terminates cleanly on
> every outcome"); conversion keeps only that its cleanup hook removes the
> temporary workspace.

#### Scenario: Workspace removed after successful job

- **GIVEN** a job completes successfully
- **WHEN** the worker finishes uploading
- **THEN** the temporary workspace directory is removed and no subprocesses remain

#### Scenario: Workspace removed after failed job

- **GIVEN** a job fails during any phase
- **WHEN** the error handler runs
- **THEN** the temporary workspace is removed automatically

### Requirement: Classify errors as retryable or permanent

The conversion adapter SHALL map each conversion failure mode onto the generic `retryable | permanent` classification that the runner consumes for retry scheduling.
Classification SHALL be a fixed property of the failure mode — it SHALL NOT depend on error-message text, runtime introspection of error objects, or caller context.

The conversion-specific mapping SHALL be:

- Missing conversion artifacts, explicit cancellation, and data-integrity violations SHALL be classified as permanent.
- Transient fetch failures, conversion-subprocess crashes, preflight failures, upload failures, and storage failures SHALL be classified as retryable.
- A child-reported failure that carries no explicit classification SHALL be treated as retryable.

> Previously: "Every failure mode SHALL be classified as either retryable or
> permanent, and the job SHALL transition to `FAILED_RETRYABLE` for retryable
> failures and `FAILED_PERM` for permanent failures." The generic
> retryable/permanent lifecycle classification and its retry semantics are now
> runner-owned (see `pipeline-stage-runtime`, "Work-units follow a generic
> lifecycle with classified terminal outcomes"); conversion keeps only its
> specific failure-mode-to-class mapping and the rule that classification is a
> fixed property of the failure mode.

#### Scenario: Permanent error for missing artifacts

- **GIVEN** conversion output artifacts are missing after the conversion phase completes
- **WHEN** the error handler processes the failure
- **THEN** the job transitions to `FAILED_PERM`

#### Scenario: Retryable error transitions job to FAILED_RETRYABLE

- **GIVEN** a transient error occurs (network failure, storage error, timeout, subprocess crash)
- **WHEN** the error handler processes it
- **THEN** the job transitions to `FAILED_RETRYABLE`

#### Scenario: Permanent error transitions job to FAILED_PERM

- **GIVEN** a non-recoverable error occurs (missing content, data-integrity violation, explicit cancellation)
- **WHEN** the error handler processes it
- **THEN** the job transitions to `FAILED_PERM`

#### Scenario: Child-reported error with no explicit retryability is treated as retryable

- **GIVEN** the conversion subprocess reports a failure without specifying retryability
- **WHEN** the error handler processes the failure
- **THEN** the job transitions to `FAILED_RETRYABLE`
