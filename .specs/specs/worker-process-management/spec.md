# Worker Process Management Specification

> Translated from Spec Kit on 2026-03-21
> Source: specs/002-worker-process-management/spec.md

## Purpose

This capability defines how the conversion worker manages process lifecycle at two levels: the worker process itself (signal handling, graceful shutdown, drain) and individual job subprocesses (isolation, cancellation, timeout, cleanup).
It covers subprocess isolation for crash containment, reliable cancellation and timeout enforcement, phase-level observability, guaranteed resource cleanup after any job outcome, and graceful worker shutdown on termination signals.

## Requirements

### Requirement: Run each job's conversion phase in an isolated subprocess

The system SHALL run each job's document conversion phase in a subprocess to isolate crashes, enable forceful termination, and reclaim memory after completion.

#### Scenario: Conversion subprocess isolates crash from parent

- **GIVEN** a conversion subprocess crashes or hangs
- **WHEN** the parent worker detects the subprocess exit
- **THEN** the parent marks the job as retryable-failed and continues processing other jobs without crashing

### Requirement: Terminate entire process groups including descendants

The system SHALL start conversion subprocesses in their own process groups and terminate the entire group when stopping a job, ensuring no orphan or grandchild processes remain.

#### Scenario: Grandchild processes terminated with parent

- **GIVEN** a conversion subprocess has spawned child processes of its own
- **WHEN** the worker terminates the job
- **THEN** all processes in the subprocess group are killed, leaving no orphan processes

### Requirement: Detect and respond to job cancellation within 2 seconds

The system SHALL poll for job cancellation in the parent process and terminate the conversion subprocess when a cancellation is detected, with detection latency not exceeding 2 seconds.

#### Scenario: Running job cancelled during conversion phase

- **GIVEN** a job is RUNNING in the conversion phase
- **WHEN** a user cancels the job via the API
- **THEN** the worker detects cancellation within 2 seconds, terminates the subprocess, and transitions the job to CANCELLED with the interrupted phase recorded

#### Scenario: Running job cancelled during upload phase

- **GIVEN** a job is RUNNING in the upload phase
- **WHEN** a user cancels the job via the API
- **THEN** the worker terminates any ongoing upload, cleans up the temporary workspace, and marks the job CANCELLED

### Requirement: Skip processing of cancelled queued jobs

The system SHALL not begin processing a job that is already CANCELLED when a worker picks it up.

#### Scenario: Cancelled queued job skipped by worker

- **GIVEN** a job is QUEUED and then cancelled before any worker starts it
- **WHEN** a worker polls for work and selects the job
- **THEN** the worker detects CANCELLED status and exits immediately without starting the subprocess

#### Scenario: Job cancelled between poll and running transition

- **GIVEN** a job is selected by the worker but cancelled before the supervised processing function begins
- **WHEN** the worker reads the current job status from the database before issuing the RUNNING update
- **THEN** it detects the CANCELLED status and exits without starting the conversion subprocess and without transitioning the job to RUNNING

### Requirement: Enforce a wall-clock timeout on job execution

The system SHALL terminate a job and mark it retryable-failed if it exceeds the configured total execution timeout, covering all phases including preflight, conversion, upload, and retry delays.

#### Scenario: Job exceeds total timeout

- **GIVEN** a job has been running for longer than the configured timeout (default: 7200 seconds)
- **WHEN** the timeout deadline is reached
- **THEN** the worker terminates the subprocess and marks the job FAILED_RETRYABLE with a timeout error code and the interrupted phase in the error message

#### Scenario: Retried job receives a fresh timeout window

- **GIVEN** a job previously failed due to timeout
- **WHEN** the job is retried
- **THEN** it receives a new full timeout window starting from the new attempt

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

### Requirement: Attempt graceful termination before forceful termination

The system SHALL send a graceful termination signal to a subprocess and wait before escalating to a forceful kill if the process does not exit.

#### Scenario: Graceful termination succeeds

- **GIVEN** a subprocess must be stopped
- **WHEN** a graceful termination signal is sent
- **THEN** the process exits within 5 seconds

#### Scenario: Forceful kill escalated after grace period

- **GIVEN** a subprocess does not exit within 5 seconds of receiving a graceful termination signal
- **WHEN** the grace period elapses
- **THEN** the worker sends a forceful kill signal and waits up to 5 additional seconds for the process to exit

### Requirement: Clean up temporary workspace on all job outcomes

The system SHALL guarantee that the temporary workspace created for a job is removed after the job finishes, regardless of whether it succeeded, failed, was cancelled, or raised an exception.

#### Scenario: Workspace removed after successful job

- **GIVEN** a job completes successfully
- **WHEN** the worker finishes uploading
- **THEN** the temporary workspace directory is removed and no subprocesses remain

#### Scenario: Workspace removed after failed job

- **GIVEN** a job fails during any phase
- **WHEN** the error handler runs
- **THEN** the temporary workspace is removed automatically

### Requirement: Classify errors as retryable or permanent

The system SHALL classify each error type as retryable or permanent via an explicit
`retryable: bool` class attribute on every exception class, and SHALL use this classification
to determine the resulting job status without relying on error message matching or `getattr`
fallbacks.

The following exception classes SHALL carry the attribute:

| Class                             | `retryable` value      | Rationale                                                                             |
| --------------------------------- | ---------------------- | ------------------------------------------------------------------------------------- |
| `ConversionArtifactsMissingError` | `False`                | Missing artifacts indicate a permanent data failure; retrying will not produce output |
| `ConversionCancelledError`        | `False`                | Job was explicitly cancelled by the user; retrying is not appropriate                 |
| `ConversionTimeoutError`          | `True`                 | Transient; fresh timeout window on retry                                              |
| `ConversionSubprocessError`       | `True`                 | Transient subprocess crash; eligible for retry                                        |
| `JobDataIntegrityError`           | `False`                | Non-recoverable data invariant violation                                              |
| `PreflightError`                  | `True`                 | Transient preflight failure; eligible for retry                                       |
| `ReportedChildError`              | `True` (class default) | Child errors default to retryable; individual instances may override                  |
| `S3Error`                         | `True`                 | Transient storage error                                                               |
| `S3UploadError`                   | `True`                 | Transient upload error                                                                |

`handle_job_error()` and `_process_job_subprocess()` SHALL read `error.retryable` directly,
without a `getattr` fallback.

#### Scenario: Permanent error for missing artifacts

- **GIVEN** conversion output artifacts are missing after the subprocess completes
- **WHEN** `handle_job_error()` processes the `ConversionArtifactsMissingError`
- **THEN** the `retryable` attribute is read directly from the exception class (value: `False`),
  and the job transitions to `FAILED_PERM`

#### Scenario: Retryable error transitions job to FAILED_RETRYABLE

- **GIVEN** a transient error occurs (network failure, S3 error, timeout)
- **WHEN** the error handler processes it
- **THEN** the job transitions to FAILED_RETRYABLE

#### Scenario: Permanent error transitions job to FAILED_PERM

- **GIVEN** a non-recoverable error occurs (missing content, data integrity violation)
- **WHEN** the error handler processes it
- **THEN** the job transitions to FAILED_PERM

#### Scenario: Child-reported error with no explicit retryability uses class default

- **GIVEN** the conversion subprocess reports a failure without specifying retryability
- **WHEN** `handle_job_error()` processes the resulting `ReportedChildError`
- **THEN** the class-level `retryable = True` default applies, classifying the job as
  `FAILED_RETRYABLE`

### Requirement: Handle SIGTERM and SIGINT by draining in-flight work before exiting

The worker process SHALL register signal handlers for SIGTERM and SIGINT that initiate a graceful shutdown sequence: stop polling for new jobs, allow in-flight jobs to complete within a bounded drain timeout, and then exit.

#### Scenario: Worker receives SIGTERM with an in-flight job

- **GIVEN** the worker is processing a job
- **WHEN** the worker process receives SIGTERM
- **THEN** the worker stops polling for new jobs and waits for the in-flight job to complete before exiting

#### Scenario: Worker receives SIGINT with an in-flight job

- **GIVEN** the worker is processing a job
- **WHEN** the worker process receives SIGINT
- **THEN** the worker behaves identically to SIGTERM — stops polling and drains in-flight work

#### Scenario: Worker receives signal while idle

- **GIVEN** the worker is idle (no jobs in progress)
- **WHEN** the worker process receives SIGTERM or SIGINT
- **THEN** the worker exits immediately without error

### Requirement: Enforce a bounded drain timeout on graceful shutdown

The worker process SHALL enforce a configurable drain timeout (default: 300 seconds) after receiving a shutdown signal.
If in-flight jobs do not complete within this timeout, the worker SHALL terminate them using the existing subprocess termination sequence and then exit.

#### Scenario: In-flight job completes within drain timeout

- **GIVEN** the worker has received SIGTERM and is draining an in-flight job
- **WHEN** the job completes before the drain timeout elapses
- **THEN** the worker exits with a zero exit code

#### Scenario: In-flight job exceeds drain timeout

- **GIVEN** the worker has received SIGTERM and is draining an in-flight job
- **WHEN** the drain timeout elapses before the job completes
- **THEN** the worker terminates the in-flight job using the existing graceful-then-forceful subprocess termination sequence, marks the job FAILED_RETRYABLE, and exits with a non-zero exit code

### Requirement: Leave no jobs in RUNNING state after worker exit

The worker process SHALL ensure that no jobs remain in RUNNING state when the worker exits, whether the exit is due to a completed drain or a drain timeout.
Jobs that cannot complete are transitioned to FAILED_RETRYABLE so they are eligible for pickup by a restarted worker.

#### Scenario: Worker exits cleanly after drain

- **GIVEN** all in-flight jobs completed during the drain period
- **WHEN** the worker process exits
- **THEN** no jobs in the database have status RUNNING that were owned by this worker

#### Scenario: Worker exits after drain timeout with forced termination

- **GIVEN** the drain timeout elapsed and in-flight jobs were forcefully terminated
- **WHEN** the worker process exits
- **THEN** the terminated jobs have status FAILED_RETRYABLE and their workspaces have been cleaned up

### Requirement: Log shutdown lifecycle events

The worker process SHALL log structured messages at each stage of the shutdown sequence for operational observability.

#### Scenario: Shutdown sequence logged

- **GIVEN** the worker receives a shutdown signal
- **WHEN** the shutdown sequence progresses
- **THEN** the worker logs: signal received (with signal name), drain started (with number of in-flight jobs), each job completion or forced termination during drain, and final exit (with exit code)

## Technical Notes

- **Implementation**: `aizk/conversion/worker/`
- **Dependencies**: conversion-worker (job data model and status transitions)
- **Subprocess model**: spawn context for clean child state; child runs conversion only; parent runs preflight and upload
- **Process group management**: subprocess sets its own process group on start; termination targets the entire group; ESRCH (group already gone) is handled gracefully
- **Cancellation polling**: parent polls database every 2 seconds using `process.join(timeout=poll_interval)`; child checks for cancellation at phase boundaries
- **Timeout tracking**: wall-clock deadline computed after job enters RUNNING state; covers all phases including upload retry delays
- **Phase values**: `starting`, `preparing_input`, `converting`, `uploading` — communicated from child to parent via inter-process queue; not persisted to database
- **Termination sequence**: SIGTERM → wait 5s → SIGKILL → wait 5s → log error if still alive
- **Workspace**: `tempfile.TemporaryDirectory` context manager in parent; path passed as string argument to subprocess; OS-level cleanup handles leaks from worker crashes
- **Error retryability**: `retryable: ClassVar[bool]` attribute on every exception class (including `S3Error` and `S3UploadError`); `handle_job_error()` reads this attribute directly rather than matching error type strings or relying on `getattr` fallbacks
- **Graceful shutdown**: signal handlers set a flag; main loop checks it before each poll; drain waits for in-flight work up to `worker_drain_timeout_seconds` (default 300); second signal forces immediate termination; force-terminated jobs transition to FAILED_RETRYABLE
- **Platform**: POSIX only (Linux, macOS); Windows not supported
