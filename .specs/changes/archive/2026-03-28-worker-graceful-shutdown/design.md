# Design: Worker Graceful Shutdown

## Context

The worker runs as a `while True` polling loop in `run_worker()`.
It currently has no signal handling — SIGTERM kills the process wherever it happens to be.
The existing spec covers job-level subprocess termination (SIGTERM → wait → SIGKILL) but not the worker process's own lifecycle.

The worker is deployed as a container process (Docker/systemd), where SIGTERM is the standard shutdown signal.
Container orchestrators send SIGTERM, wait a grace period (typically 30s for Docker, configurable in K8s), then SIGKILL.
The drain timeout must fit within the orchestrator's grace period.

Currently the worker processes one job at a time (item 2c will add concurrency).
The shutdown design must work for both single-job and future multi-job scenarios.

## Decisions

### Decision: Signal handler sets a flag, main loop checks it

**Chosen:** Signal handlers set an `Event` or boolean flag.
The polling loop checks the flag before each poll cycle and exits when set.
In-flight work continues until completion or drain timeout.

**Rationale:** Signal handlers in Python are restricted — they run in the main thread and cannot perform I/O, acquire locks, or call most library functions safely.
A flag-based approach keeps the handler minimal and moves all shutdown logic to the main loop where it can safely interact with the database and subprocesses.

**Alternatives considered:**

- Raise an exception from the signal handler: unsafe in the middle of database transactions or subprocess management; hard to guarantee cleanup
- Use `asyncio` cancellation: the worker is synchronous; would require a rewrite unrelated to this change

### Decision: Drain timeout defaults to 300 seconds

**Chosen:** 300 seconds (5 minutes) as the default drain timeout, configurable via `ConversionConfig`.

**Rationale:** Conversion jobs can take up to 2 hours (`worker_job_timeout_seconds=7200`), but a 2-hour drain would be unreasonable for deployments. 300 seconds gives most in-progress uploads and short conversions time to finish.
Long-running conversions will be terminated and retried.
Operators must set their orchestrator's stop grace period to at least `drain_timeout + 10s` (the subprocess termination sequence budget).

**Alternatives considered:**

- Match the job timeout (7200s): too long for deployment workflows; defeats the purpose of graceful shutdown
- Short timeout (30s): insufficient for S3 uploads of large artifacts; would force-kill most in-flight work
- No timeout (wait forever): hangs the deployment if a job is stuck

### Decision: Terminated jobs transition to FAILED_RETRYABLE

**Chosen:** Jobs force-terminated during drain are marked `FAILED_RETRYABLE`, not `FAILED_PERM`.

**Rationale:** The termination is caused by operator action (deployment), not by a problem with the job itself.
The job should be retried by the next worker instance.
This is consistent with how the existing spec handles timeout-terminated jobs.

**Alternatives considered:**

- FAILED_PERM: incorrect — the job didn't fail due to its own content or logic
- Leave as RUNNING for stale recovery: adds 30 minutes of unnecessary delay; the whole point of graceful shutdown is to avoid this

## Architecture

```text
                    SIGTERM / SIGINT
                          |
                          v
              +------------------------+
              |  signal handler:       |
              |  set shutdown_event    |
              +------------------------+
                          |
                          v
              +------------------------+
              |  main loop checks      |
              |  shutdown_event        |
              |  before each poll      |
              +------------------------+
                    |             |
              (idle)             (in-flight job)
                |                     |
                v                     v
           exit(0)          +-------------------+
                            | wait for job      |
                            | up to drain       |
                            | timeout           |
                            +-------------------+
                              |             |
                        (completed)   (timeout)
                            |             |
                            v             v
                         exit(0)    terminate subprocess
                                    mark FAILED_RETRYABLE
                                    cleanup workspace
                                    exit(1)
```

## Risks

- **Orchestrator grace period too short:** If the container runtime's stop timeout is less than the drain timeout, the worker gets SIGKILL mid-drain.
  Mitigation: document that the orchestrator grace period must exceed `worker_drain_timeout_seconds + 10`.
  Log the configured drain timeout at startup.
- **Second signal during drain:** A second SIGTERM/SIGINT during drain could mean "stop waiting, exit now."
  Mitigation: treat a second signal as an immediate forced termination (skip remaining drain, terminate subprocesses, exit).
  This matches common Unix daemon behavior.
- **Future concurrency interaction:** Item 2c will add parallel job processing.
  The drain logic must wait for all in-flight jobs, not just one.
  Mitigation: design the shutdown flag and drain loop to be concurrency-agnostic — check "any in-flight work remains" rather than "the single job is done."
