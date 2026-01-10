# Feature Specification: Robust Worker Process Management

**Feature Branch**: `002-worker-process-management`
**Created**: 2026-01-10
**Status**: Draft
**Input**: Refactor the conversion worker to provide robust job lifecycle management with cancellation, timeout, and cleanup guarantees for long-running conversion operations.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Cancel a Running Job (Priority: P1)

An administrator cancels a running job and the worker interrupts it within 5 seconds, with no zombie processes or lingering resources.

**Why this priority**: Core operational requirement. Without reliable cancellation, users cannot stop runaway jobs, leading to resource exhaustion.

**Independent Test**: Submit a long-running conversion job, cancel it mid-execution via API, verify job status changes to CANCELLED within 5 seconds, and confirm no subprocess remains.

**Acceptance Scenarios**:

1. **Given** a job is RUNNING in the conversion phase, **When** user cancels it via API, **Then** the worker detects cancellation within 2 seconds, terminates the subprocess, and the job transitions to CANCELLED with phase captured in error message
2. **Given** a job is RUNNING in the upload phase, **When** user cancels it, **Then** the worker terminates any ongoing upload, cleans up the temporary workspace, and marks job CANCELLED
3. **Given** a job subprocess spawns child processes, **When** the worker terminates the job, **Then** all child and grandchild processes are killed via process group termination, leaving no orphans
4. **Given** a job is terminated via SIGTERM, **When** the process does not exit within 5 seconds, **Then** the worker escalates to SIGKILL to force termination

---

### User Story 2 - Prevent Starting Cancelled Queued Jobs (Priority: P1)

A user cancels a queued job before a worker picks it up, and the worker never starts processing it.

**Why this priority**: Prevents wasting resources on unwanted work and ensures user intent is respected immediately.

**Independent Test**: Submit a job, immediately cancel it while QUEUED, verify worker polls but skips the cancelled job.

**Acceptance Scenarios**:

1. **Given** a job is QUEUED, **When** user cancels it before any worker picks it up, **Then** the job transitions to CANCELLED and no worker ever starts processing it
2. **Given** a job is selected by `poll_and_process_jobs` but is cancelled between SELECT and RUNNING state transition, **When** the worker enters `process_job_supervised`, **Then** it detects CANCELLED status and exits immediately without starting the subprocess

---

### User Story 3 - Timeout Long-Running Jobs (Priority: P1)

A job that exceeds the configured timeout (7200 seconds wall-clock) is forcefully terminated and marked FAILED_RETRYABLE with the interrupted phase recorded.

**Why this priority**: Prevents indefinite resource consumption from stuck jobs and ensures SLA compliance.

**Independent Test**: Configure a short timeout (e.g., 10 seconds), submit a job that takes longer, verify timeout triggers and job is terminated.

**Acceptance Scenarios**:

1. **Given** a job runs for longer than 7200 seconds total (including all phases and retry delays), **When** the timeout deadline is reached, **Then** the worker terminates the subprocess and marks the job FAILED_RETRYABLE with error_code='conversion_timeout' and phase in error message
2. **Given** a job times out during the conversion phase, **When** recorded in the database, **Then** the error message includes 'exceeded its runtime during converting' to indicate the interrupted phase
3. **Given** a job times out during the upload retry loop, **When** recorded, **Then** the error message includes 'exceeded its runtime during uploading'
4. **Given** a job is FAILED_RETRYABLE due to timeout, **When** it is retried, **Then** it receives a fresh 7200-second timeout window starting from the new attempt

---

### User Story 4 - Report Processing Phase (Priority: P2)

A worker reports the current phase of each job for observability and debugging, allowing operators to understand where jobs are spending time.

**Why this priority**: Operational visibility aids troubleshooting and capacity planning, but the core lifecycle management (cancel/timeout) must work first.

**Independent Test**: Monitor logs while processing a job, verify phase transitions are logged with job ID and timestamp.

**Acceptance Scenarios**:

1. **Given** a worker starts processing a job, **When** it transitions between phases, **Then** it logs entries: "Job {id} entered phase preflight", "Job {id} entered phase preparing_input", "Job {id} entered phase converting", "Job {id} entered phase uploading"
2. **Given** a job is interrupted, **When** logged or recorded, **Then** the final message includes the last known phase (e.g., "Job 42 cancelled during converting")
3. **Given** multiple workers process jobs concurrently, **When** viewing logs, **Then** each log entry includes the job ID to enable filtering and correlation

---

### User Story 5 - Clean Up Resources After Job Completion (Priority: P1)

After any job completion (success, failure, cancellation, timeout), no subprocess remains running and no temporary files persist.

**Why this priority**: Resource leaks cause operational failures over time. Cleanup must be guaranteed for system reliability.

**Independent Test**: Process a job, kill the worker mid-execution, verify no orphan processes or temp directories remain after recovery.

**Acceptance Scenarios**:

1. **Given** a job completes successfully, **When** the worker finishes uploading, **Then** the temporary workspace directory is removed and no subprocesses remain
2. **Given** a job fails during conversion, **When** the error handler runs, **Then** the temporary workspace is cleaned up automatically via context manager
3. **Given** a worker crashes while processing a job, **When** a new worker starts and runs stale job recovery, **Then** it marks the job FAILED_RETRYABLE but does not clean up old temp directories (assumes OS reboot or tmpwatch handles this)
4. **Given** a job subprocess spawns descendants, **When** the job is terminated, **Then** process group termination kills all descendants, preventing zombie processes

---

## Functional Requirements *(mandatory)*

### FR-1: Subprocess Isolation

The worker MUST run each job's conversion phase in a subprocess to isolate crashes, enable termination, and reclaim memory.

**Rationale**: Conversion libraries (docling) may crash or hang. Subprocess isolation prevents one bad job from taking down the entire worker.

**Implementation Constraints**:

- Use `multiprocessing.spawn` context to ensure clean child state
- Child process runs `_convert_job_artifacts()` (input preparation + conversion)
- Parent process runs preflight (bookmark fetch/validation) and upload phases

### FR-2: Process Group Management

The worker MUST start subprocesses in their own process group and terminate the entire group to handle grandchild processes.

**Rationale**: Conversion libraries may spawn additional subprocesses. Terminating only the immediate child leaves orphans.

**Implementation Constraints**:

- Call `os.setpgrp()` at the start of the subprocess target function
- Use `os.killpg(pgid, SIGTERM)` and `os.killpg(pgid, SIGKILL)` for termination
- Handle ESRCH (process group already gone) gracefully

### FR-3: Cancellation Detection

The parent process MUST poll the database for job cancellation every 2 seconds and terminate the subprocess if cancelled.

**Rationale**: User-initiated cancellation must be respected within acceptable latency (0-5 seconds).

**Implementation Constraints**:

- Parent checks `_is_job_cancelled(job_id, engine)` during `process.join(timeout=poll_interval_seconds)` loop
- Child checks `_raise_if_cancelled(job_id, engine)` at phase boundaries (before conversion, after conversion)
- On cancellation detection, parent calls `process.terminate()` → wait 5s → `process.kill()`

### FR-4: Timeout Enforcement

The parent process MUST terminate a job if it exceeds the configured `worker_job_timeout_seconds` (default 7200).

**Rationale**: Prevents indefinite resource consumption and enforces processing SLAs.

**Implementation Constraints**:

- Timeout covers total wall-clock time: preflight + subprocess runtime + upload + retry delays
- Deadline is computed as `time.monotonic() + timeout_seconds` after job enters RUNNING state
- On timeout, parent terminates subprocess and calls `handle_job_error()` with `ConversionTimeoutError(phase=last_phase)`

### FR-5: Phase Tracking

The subprocess MUST report phase transitions to the parent via a multiprocessing Queue.

**Rationale**: Provides visibility into job progress and enables accurate "interrupted during X" reporting.

**Implementation Constraints**:

- Phases: `starting`, `preparing_input`, `converting`, `uploading`
- Child calls `_report_status(queue, event="phase", message=phase_name)` at each transition
- Parent reads messages via `queue.get_nowait()` and logs transitions
- Phase information is NOT persisted to database, only logged

### FR-6: Graceful and Forceful Termination

The worker MUST attempt graceful termination (SIGTERM) before forceful termination (SIGKILL).

**Rationale**: Allows subprocesses to clean up resources when possible, but guarantees termination if unresponsive.

**Implementation Constraints**:

- Send SIGTERM, wait 5 seconds via `process.join(timeout=5.0)`
- If still alive, send SIGKILL and wait 5 seconds
- If still alive after SIGKILL, log error but continue (process is unkillable, likely kernel issue)

### FR-7: Temporary Workspace Cleanup

The parent process MUST use a `tempfile.TemporaryDirectory` context manager for the job workspace, ensuring cleanup on success, failure, or exception.

**Rationale**: Temporary files must not persist beyond job processing to prevent disk exhaustion.

**Implementation Constraints**:

- Create temp directory in parent before spawning subprocess
- Pass directory path to subprocess as string argument
- Context manager guarantees cleanup even if parent raises exception
- OS-level tmpwatch/systemd-tmpfiles handles cleanup if worker crashes before context exit

### FR-8: Error Retryability Classification

Each exception type MUST declare whether it represents a retryable or permanent failure.

**Rationale**: Centralized retry logic requires error types to carry their own semantics rather than relying on brittle string matching.

**Implementation Constraints**:

- Add `retryable: bool` attribute to exception base class (default `True`)
- Permanent errors (e.g., `JobDataIntegrityError`, `BookmarkContentError`) set `retryable = False`
- Transient errors (e.g., `S3Error`, `FetchError`, `ConversionTimeoutError`) set `retryable = True`
- `handle_job_error()` uses `getattr(error, "retryable", True)` instead of string set lookup

### FR-9: Queued Job Cancellation Handling

The worker MUST skip processing a job if it is CANCELLED before entering RUNNING state.

**Rationale**: Prevents starting unwanted work after user cancels a queued job.

**Implementation Constraints**:

- `poll_and_process_jobs()` filters for `status IN (QUEUED, FAILED_RETRYABLE)` only
- `process_job_supervised()` checks `job.status in {SUCCEEDED, CANCELLED}` and exits early
- Acceptable race condition: If cancelled between poll and RUNNING transition, subprocess starts but exits immediately on first cancellation check

### FR-10: Interruption Phase Reporting

When a job is interrupted (cancelled or timed out), the error record MUST include the phase that was active at interruption.

**Rationale**: Aids debugging and helps identify which operations are prone to hanging or timeout.

**Implementation Constraints**:

- Parent tracks `last_phase` by consuming messages from status queue
- `ConversionTimeoutError.__init__(message, phase)` stores interrupted phase
- Error messages use format: "Job {id} exceeded its runtime during {phase}"
- Cancellation logs: "Job {id} cancelled during {phase}"

---

## Success Criteria *(mandatory)*

1. **Cancellation responsiveness**: 95% of running jobs transition to CANCELLED within 5 seconds of API cancellation request
2. **No zombie processes**: After any job termination (success, failure, cancel, timeout), zero orphan processes remain (verified via process table inspection)
3. **Timeout accuracy**: Jobs exceeding `worker_job_timeout_seconds` are terminated within ±10 seconds of the configured deadline
4. **Temporary file cleanup**: After 100 job executions (mix of success/failure/cancel), zero temporary directories persist in `/tmp` (excluding active jobs)
5. **Phase visibility**: 100% of jobs log all phase transitions with correct job ID and timestamp
6. **Queued job cancellation**: 100% of jobs cancelled while QUEUED never enter RUNNING state
7. **Error retryability correctness**: 100% of retryable errors (timeout, network) transition to FAILED_RETRYABLE; 100% of permanent errors (integrity, missing content) transition to FAILED_PERM

---

## Key Entities *(optional)*

### JobPhase (not persisted, runtime only)

Represents the current phase of job execution.

**Properties**:

- `starting`: Worker has begun preflight (parent process)
- `preparing_input`: Subprocess is fetching and validating source content
- `converting`: Subprocess is running docling conversion
- `uploading`: Parent is uploading artifacts to S3

**Notes**: Phases are not stored in database, only reported via logging and captured in error messages.

---

## User Experience *(optional)*

No direct UI changes. Phase reporting appears in logs consumed by operators and SRE tools.

---

## Edge Cases & Error Handling *(optional)*

### Edge Case 1: Subprocess Terminates Abnormally

**Scenario**: Child process crashes with segfault or other unhandled exception.

**Handling**:

- Parent checks `process.exitcode != 0`
- Raises `ConversionSubprocessError` with exit code
- `handle_job_error()` marks job FAILED_RETRYABLE (crashes are transient)

### Edge Case 2: Worker Crashes Mid-Processing

**Scenario**: Parent process killed (OOM, server restart) while job is RUNNING.

**Handling**:

- Job remains RUNNING in database
- Stale job recovery runs periodically (every `worker_stale_job_check_seconds`)
- `recover_stale_running_jobs()` marks jobs stale after `worker_stale_job_minutes`
- Temp directories are NOT cleaned up by recovery (relies on OS-level cleanup)

### Edge Case 3: Process Group Already Terminated

**Scenario**: Attempt to kill process group after subprocess exits normally.

**Handling**:

- `os.killpg()` raises `ProcessLookupError` (errno ESRCH)
- Catch and ignore this exception (expected when process group is gone)

### Edge Case 4: Timeout During Upload Retry Loop

**Scenario**: Job times out while retrying S3 upload with exponential backoff.

**Handling**:

- Timeout check occurs at start of each upload retry iteration
- If deadline exceeded, exit retry loop and call `handle_job_error()`
- Job marked FAILED_RETRYABLE (upload failures are transient)

### Edge Case 5: Cancellation After Subprocess Completes

**Scenario**: User cancels job after conversion completes but before upload starts.

**Handling**:

- Parent checks `_is_job_cancelled()` before entering upload phase
- If cancelled, logs "Job {id} cancelled before upload" and returns without uploading
- Job remains CANCELLED; no S3 artifacts published

---

## Dependencies & Assumptions *(optional)*

### Dependencies

- **Python 3.11+**: For `multiprocessing` improvements and exception groups
- **SQLite with WAL mode**: For concurrent read/write (BEGIN IMMEDIATE)
- **Unix-like OS**: Process group management requires POSIX signals (Linux, macOS)
- **tmpwatch or systemd-tmpfiles**: For cleaning leaked temp directories after worker crash

### Assumptions

1. **Single-worker deployment initially**: Atomic job claiming relies on SQLite's BEGIN IMMEDIATE transaction; scaling to multiple workers requires testing under contention
2. **Docling may spawn subprocesses**: Process group management handles this case even if current docling version doesn't, future-proofing the design
3. **S3 writes are atomic**: Upload retry logic assumes S3 PUT operations are atomic (either fully uploaded or not present)
4. **Worker has privilege to send signals**: Assumes worker runs with sufficient privilege to SIGTERM/SIGKILL its own children
5. **Cancellation latency tolerance is 0-5 seconds**: Users accept this latency; sub-second responsiveness is not required

---

## Out of Scope *(optional)*

The following are explicitly excluded from this feature:

1. **Real-time cancellation (signals to child)**: Child does not receive SIGUSR1 or shared event for instant cancellation; relies on DB polling at checkpoints
2. **Per-phase timeouts**: Single timeout covers entire job; no separate limits for conversion vs. upload
3. **Persistent phase tracking in database**: Phases are logged but not stored in `conversion_jobs` table
4. **Windows support**: Process group management uses POSIX APIs (Linux/macOS only)
5. **Graceful child shutdown acknowledgment**: Child termination is forceful (SIGTERM/SIGKILL); no handshake protocol for graceful exit
6. **Rollback of partial S3 uploads**: Assumes S3 PUT is atomic; no cleanup of incomplete multipart uploads
7. **Distributed tracing integration**: Phase transitions are logged but not instrumented for OpenTelemetry/Jaeger

---

## Open Questions *(optional)*

1. **Does docling spawn subprocesses?** — If yes, process group management is essential; if no, it's defensive but harmless. **Action**: Investigate docling internals or test with `pstree` during conversion.

2. **Should phases be persisted to database for analytics?** — Current design logs only. If phase duration metrics are needed later, consider adding `current_phase` and `phase_history` JSON column to `conversion_jobs`.

3. **Should upload phase have a separate timeout?** — Current design uses single 7200s timeout. If uploads routinely take >1 hour, consider splitting into `worker_conversion_timeout_seconds` and `worker_upload_timeout_seconds`.

4. **How to handle worker crash temp directory cleanup?** — Current design relies on OS-level tmpwatch. Consider: (a) tracking temp dirs in DB, or (b) using a named prefix for worker temp dirs and cleaning on startup.

---

## Assumptions & Constraints *(mandatory)*

### Technical Constraints

- **Subprocess isolation limits**: Cannot share complex Python objects between parent and child; must serialize via JSON or pass file paths
- **Signal delivery latency**: SIGTERM delivery is not instant; assume 1-50ms depending on system load
- **SQLite concurrency**: BEGIN IMMEDIATE provides write lock but may cause contention with multiple workers
- **Temp directory location**: Uses default `tempfile` location (typically `/tmp`); may fill disk if many large jobs run concurrently

### Architectural Constraints

- **Parent/child responsibility boundary**: Parent handles I/O that must survive child crash (preflight, upload); child handles crash-prone work (conversion)
- **Error classification principle**: Exceptions encode their own retry semantics via `retryable` attribute rather than centralized lookup
- **Phase visibility without persistence**: Phases are observable via logs but not queryable via database; sufficient for current needs

---

## Additional Notes *(optional)*

### Design Rationale

This design prioritizes **robustness and operational clarity** over complexity:

- **Subprocess isolation** trades performance (process spawn overhead) for reliability (crash containment)
- **Process groups** add minimal complexity but eliminate entire class of orphan process bugs
- **Polling-based cancellation** is simple and meets latency requirements without signal handler complexity
- **Wall-clock timeout** is easier to reason about than "active time" or per-phase limits

### Migration Path

This feature refines the existing worker without breaking changes:

1. Current staged changes provide subprocess foundation
2. Add process group management (small change to subprocess startup)
3. Add `retryable` attribute to exceptions (backward-compatible via `getattr` with default)
4. No database schema changes required
5. Existing tests continue to work with `_InlineContext` mocks

### Future Enhancements

Deferred for later consideration:

- **Persistent phase tracking**: If operators need phase duration analytics, add DB column
- **Distributed worker coordination**: If scaling beyond single worker, use Redis/PostgreSQL for job claims
- **Structured logging**: Emit JSON logs with job_id, phase, duration for ingestion into observability platforms
- **Health checks**: Expose worker liveness/readiness endpoints for container orchestration
