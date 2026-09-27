# Worker Process Management Specification

> Translated from Spec Kit on 2026-03-21
> Source: specs/002-worker-process-management/spec.md

## Purpose

This capability defines how the conversion worker manages process lifecycle at two levels: the worker process itself (signal handling, graceful shutdown, drain) and individual job subprocesses (isolation, cancellation, timeout, cleanup).
It covers subprocess isolation for crash containment, reliable cancellation and timeout enforcement, phase-level observability, guaranteed resource cleanup after any job outcome, and graceful worker shutdown on termination signals.

## Requirements

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

#### Scenario: Conversion crash does not affect other jobs and is retried

- **GIVEN** a job's conversion subprocess crashes
- **WHEN** the worker observes the failure
- **THEN** that job is `failed` with a retry wait set, and other in-flight jobs continue processing without disruption

#### Scenario: A hang stopped by the wall-clock limit times out

- **GIVEN** a job's conversion subprocess hangs until the job's wall-clock timeout
- **WHEN** the runner terminates it
- **THEN** that job is `timed_out`, its subprocess and descendants are gone, and other in-flight jobs continue processing without disruption

### Requirement: Report job phase transitions for observability

The system SHALL log phase transitions as a job progresses through its execution stages, and SHALL include the last known phase in error messages when a job is interrupted.

#### Scenario: Phase transitions logged with job identifier

- **GIVEN** a worker is processing a job
- **WHEN** the job transitions between phases (preflight, preparing input, converting, uploading)
- **THEN** each transition is logged with the job identifier and timestamp

#### Scenario: Interrupted phase recorded in error

- **GIVEN** a job is cancelled or timed out
- **WHEN** the error is recorded
- **THEN** the error message includes the phase that was active at the time of interruption

### Requirement: Clean up temporary workspace on all job outcomes

The conversion stage's primary transient resource is the temporary workspace created for a job; the conversion adapter SHALL remove that workspace on every terminal outcome — succeeded, failed, cancelled, or timed out — by scoping it to the adapter's unit-of-work execution, so no workspace survives the unit regardless of how it ends.
The generic guarantee that transient resources are released on every terminal outcome is owned by `pipeline-stage-runtime`; this requirement retains only the conversion-specific obligation to remove the temporary workspace.

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
Classification SHALL be a fixed property of the failure mode, declared by the error type or by the conversion subprocess's failure report — it SHALL NOT depend on error-message text or caller context.
A classified failure SHALL leave the job `failed`: retryable with a retry wait set, permanent with no retry wait and the job finished.
A wall-clock timeout is not a classified failure; it SHALL leave the job `timed_out`, terminal until an operator retries it.
An explicit cancellation is not a classified failure; it SHALL leave the job `cancelled`.

The conversion-specific mapping SHALL be:

- Missing conversion artifacts and data-integrity violations SHALL be classified as permanent.
- Transient fetch failures, conversion-subprocess crashes, preflight failures, upload failures, and storage failures SHALL be classified as retryable.
- A child-reported failure that carries no explicit classification SHALL be treated as retryable.

#### Scenario: Permanent error for missing artifacts

- **GIVEN** conversion output artifacts are missing after the conversion phase completes
- **WHEN** the error handler processes the failure
- **THEN** the job is `failed` with no retry wait and a finished time

#### Scenario: Retryable error sets a retry wait

- **GIVEN** a transient error occurs (network failure, storage error, subprocess crash)
- **WHEN** the error handler processes it
- **THEN** the job is `failed` with a retry wait set

#### Scenario: Permanent error leaves no retry wait

- **GIVEN** a non-recoverable error occurs (missing content, data-integrity violation)
- **WHEN** the error handler processes it
- **THEN** the job is `failed` with no retry wait and a finished time

#### Scenario: Child-reported error with no explicit retryability is treated as retryable

- **GIVEN** the conversion subprocess reports a failure without specifying retryability
- **WHEN** the error handler processes the failure
- **THEN** the job is `failed` with a retry wait set

#### Scenario: Wall-clock timeout is timed out and waits for an operator

- **GIVEN** a job running past its wall-clock timeout
- **WHEN** the runner terminates it
- **THEN** the job is `timed_out`, is not retried automatically, and becomes `queued` again only when an operator retries it

## Technical Notes

- **Implementation**: `aizk/conversion/processing/` (`worker.py`, `supervision.py`) and the child entry in `aizk/conversion/wiring/subprocess.py`

- **Dependencies**: conversion-worker (job data model and status transitions)

- **Subprocess model**: spawn context for clean child state; child runs conversion only; parent runs preflight and upload

- **Process group management**: subprocess sets its own process group on start; termination targets the entire group; ESRCH (group already gone) is handled gracefully

- **Cancellation polling**: parent polls database every 2 seconds using `process.join(timeout=poll_interval)`; child checks for cancellation at phase boundaries

- **Timeout tracking**: wall-clock deadline computed after the job enters `running`; covers all phases including upload retry delays

- **Phase values**: `starting`, `preparing_input`, `converting`, `uploading` — communicated from child to parent via inter-process queue; not persisted to database

- **Termination sequence**: SIGTERM → wait 5s → SIGKILL → wait 5s → log error if still alive

- **Workspace**: `tempfile.TemporaryDirectory` context manager in parent; path passed as string argument to subprocess; OS-level cleanup handles leaks from worker crashes

- **Error retryability**: `retryable: ClassVar[bool]` attribute on every exception class; the conversion handler's outcome mapping and the child entry (`_process_job_subprocess()`) read this attribute rather than matching error type strings; an exception without it counts as retryable.
  Per-class values:

  | Class | `retryable` | Rationale | | --------------------------------- | ---------------------- | ------------------------------------------------------------------------------------- | | `ConversionArtifactsMissingError` | `False` | Missing artifacts indicate a permanent data failure; retrying will not produce output | | `ConversionCancelledError` | `False` | Job was explicitly cancelled by the user; retrying is not appropriate | | `ConversionTimeoutError` | `True` | Transient; fresh timeout window on retry | | `ConversionSubprocessError` | `True` | Transient subprocess crash; eligible for retry | | `JobDataIntegrityError` | `False` | Non-recoverable data invariant violation | | `PreflightError` | `True` | Transient preflight failure; eligible for retry | | `ReportedChildError` | `True` (class default) | Child errors default to retryable; individual instances may override | | `S3Error` | `True` | Transient storage error | | `S3UploadError` | `True` | Transient upload error |

- **Concurrency**: main thread polls and dispatches jobs to a ThreadPoolExecutor (`worker_concurrency`, default 4); GPU subprocess spawning gated by a semaphore (`worker_gpu_concurrency`, default 1) to prevent GPU OOM; preflight and upload phases run outside the semaphore

- **Graceful shutdown**: signal handlers set a flag; main loop checks it before each poll; drain waits for all in-flight jobs up to `worker_drain_timeout_seconds` (default 300) plus a 15-second buffer; second signal forces immediate termination; force-terminated jobs transition to `failed` with error code `worker_drain_terminated`, and the next worker claims them again until the attempt limit is reached

- **Platform**: POSIX only (Linux, macOS); Windows not supported
