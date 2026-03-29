# Delta for Worker Process Management

## MODIFIED Requirements

### Requirement: Handle SIGTERM and SIGINT by draining in-flight work before exiting

The worker process SHALL register signal handlers for SIGTERM and SIGINT that initiate a graceful shutdown sequence: stop polling for new jobs, allow all in-flight jobs to complete within a bounded drain timeout, and then exit. (Previously: "allow in-flight jobs to complete" — singular job implied by single-threaded architecture.)

#### Scenario: Worker receives SIGTERM with multiple in-flight jobs

- **GIVEN** the worker is processing multiple jobs concurrently
- **WHEN** the worker process receives SIGTERM
- **THEN** the worker stops claiming new jobs and waits for all in-flight jobs to complete before exiting

#### Scenario: Worker receives signal while idle

- **GIVEN** the worker is idle (no jobs in progress)
- **WHEN** the worker process receives SIGTERM or SIGINT
- **THEN** the worker exits immediately without error

### Requirement: Enforce a bounded drain timeout on graceful shutdown

The worker process SHALL enforce a configurable drain timeout (default: 300 seconds) after receiving a shutdown signal.
If any in-flight jobs do not complete within this timeout, the worker SHALL terminate them using the existing subprocess termination sequence and then exit. (Previously: "If in-flight jobs do not complete" — singular job implied.)

#### Scenario: All in-flight jobs complete within drain timeout

- **GIVEN** the worker has received SIGTERM and is draining multiple in-flight jobs
- **WHEN** all jobs complete before the drain timeout elapses
- **THEN** the worker exits with a zero exit code

#### Scenario: Some in-flight jobs exceed drain timeout

- **GIVEN** the worker has received SIGTERM and is draining multiple in-flight jobs
- **WHEN** the drain timeout elapses before all jobs complete
- **THEN** the worker terminates remaining in-flight jobs using the existing graceful-then-forceful subprocess termination sequence, marks them FAILED_RETRYABLE, and exits with a non-zero exit code

### Requirement: Leave no jobs in RUNNING state after worker exit

The worker process SHALL ensure that no jobs remain in RUNNING state when the worker exits, whether the exit is due to a completed drain or a drain timeout.
Jobs that cannot complete are transitioned to FAILED_RETRYABLE so they are eligible for pickup by a restarted worker. (Previously: unchanged in requirement text, but scenarios now cover multiple concurrent jobs.)

#### Scenario: Worker exits cleanly after draining multiple jobs

- **GIVEN** all in-flight jobs completed during the drain period
- **WHEN** the worker process exits
- **THEN** no jobs in the database have status RUNNING that were owned by this worker

#### Scenario: Worker exits after drain timeout with forced termination of multiple jobs

- **GIVEN** the drain timeout elapsed and multiple in-flight jobs were forcefully terminated
- **WHEN** the worker process exits
- **THEN** all terminated jobs have status FAILED_RETRYABLE and their workspaces have been cleaned up
