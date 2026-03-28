# Delta for Worker Process Management

## ADDED Requirements

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
