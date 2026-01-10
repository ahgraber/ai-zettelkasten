# Implementation Plan: Robust Worker Process Management

**Branch**: `feature/ingest` (in-progress refactor) | **Date**: 2026-01-10 | **Spec**: [spec.md](./spec.md)\
**Input**: Feature specification from `/specs/002-worker-process-management/spec.md`

## Summary

Refactor the conversion worker to provide robust job lifecycle management with subprocess isolation, cancellation (within 5s), timeout enforcement (7200s wall-clock), phase reporting, and guaranteed cleanup. Use process groups to kill grandchild processes, poll DB for cancellation, enforce deadline-based timeout, report phases via `mp.Queue`, and ensure temp directory cleanup via context manager. Implement exception `retryable` attribute to replace string-based error classification.

## Technical Context

**Language/Version**: Python 3.11+ (managed via uv)\
**Primary Dependencies**: multiprocessing (stdlib), SQLModel, existing conversion stack (docling, S3Client)\
**Platform**: Linux/macOS (requires POSIX process groups via `os.setpgrp()` and `os.killpg()`)\
**Testing**: pytest with mocked subprocesses for unit tests, real subprocesses for integration tests\
**Existing State**: Worker already has `process_job_supervised()` with subprocess spawn, cancellation polling, timeout checking, and phase reporting—this plan refines and hardens the implementation

**Performance Goals**:

- Cancellation latency: 95% within 5 seconds
- Timeout accuracy: ±10 seconds of configured deadline
- Zero zombie processes after termination
- Zero leaked temp directories after 100 jobs

**Constraints**:

- Single worker initially (SQLite BEGIN IMMEDIATE for atomic job claims)
- Subprocess spawn overhead: ~50-200ms per job
- Signal delivery latency: 1-50ms depending on system load
- Temp directory cleanup relies on context manager + OS-level tmpwatch for crash recovery

**Scale/Scope**:

- 4 concurrent workers (each runs `poll_and_process_jobs()` loop)
- Jobs may run up to 7200 seconds (2 hours)
- Conversion operations are CPU/memory intensive (docling)
- Expected throughput: hundreds of jobs per day

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **G1 Data provenance**:

  - Sources: Job records in conversion_jobs table (status, attempts, error tracking)
  - Metadata: Phase transitions logged with job_id and timestamp
  - No new data storage: Phase information is logged, not persisted to DB
  - Traceability: Error messages include interrupted phase for debugging
  - **Status**: ✅ PASS (no new data sources, existing job records maintain provenance)

- **G2 Reproducibility**:

  - Deterministic behavior: Cancellation/timeout logic is deterministic given wall-clock time and DB state
  - No randomness: Process termination uses fixed timeouts (5s SIGTERM → SIGKILL)
  - Idempotency: Jobs can be retried with identical behavior after timeout/cancellation
  - Environment: Process group creation is standard POSIX behavior
  - **Status**: ✅ PASS

- **G3 Test-first**:

  - Unit tests: Mock subprocess behavior to test cancellation/timeout logic without real processes
  - Integration tests: Spawn real subprocesses to verify process group termination, signal handling, cleanup
  - Contract tests: Verify job state transitions (QUEUED → RUNNING → CANCELLED/SUCCEEDED/FAILED_RETRYABLE)
  - Regression tests: Edge cases (subprocess crash, worker crash, timeout during upload, cancellation race)
  - Gate: Tests written and failing (red phase) before implementation
  - **Status**: ✅ PASS (tests planned for both unit and integration levels)

- **G4 Privacy & safety**:

  - No PII: Only job metadata (status, phase, error messages) in logs
  - Process management: Uses standard OS facilities (process groups, signals)
  - Resource safety: Temp directory cleanup prevents disk exhaustion
  - No external APIs: All operations are local (DB, filesystem, subprocess management)
  - **Status**: ✅ PASS

- **G5 Observability & versioning**:

  - Structured logging: All phase transitions include job_id, phase name, timestamp
  - Metrics: Add cancellation_count, timeout_count, zombie_process_count gauges
  - Error tracking: ConversionTimeoutError includes interrupted phase
  - No schema changes: Phase information is logged, not stored in DB
  - **Status**: ✅ PASS

- **G6 ADRs**:

  - ADR-001: Process group management over shared events/signals (rationale: handles grandchildren, no IPC complexity)
  - ADR-002: DB polling for cancellation over real-time signals (rationale: meets latency requirement, simpler than signal handlers)
  - ADR-003: Wall-clock timeout over per-phase limits (rationale: easier to reason about, single deadline)
  - ADR-004: Exception attribute for retryability over string lookup (rationale: type-safe, self-documenting)
  - **Status**: ✅ PASS (design decisions documented in this plan)

- **G7 Governance**:

  - Changelog: CHANGELOG.md updated with `fix(worker):` entries for robustness improvements
  - Semantic Versioning: Bug fixes and improvements warrant PATCH bump (or bundled with next MINOR)
  - Conventional Commits: PR uses `fix(worker):` or `feat(worker):` prefix depending on scope
  - **Status**: ✅ PASS

- **G8 Preconditions**:

  - ✅ spec.md complete with requirements and acceptance criteria
  - ✅ plan.md complete with implementation roadmap (this file)
  - ✅ Current worker implementation staged (subprocess, timeout, cancellation foundation exists)
  - ✅ Ready for Phase 0 research and Phase 1 implementation

## Architecture Decision Records

### ADR-001: Process Group Management

**Context**: Conversion operations (docling) may spawn subprocesses. When terminating a job, we need to kill the entire process tree.

**Options**:

1. Process groups (`os.setpgrp()` + `os.killpg()`) — standard POSIX pattern
2. `prctl PR_SET_PDEATHSIG` — Linux-only, automatic on parent death
3. Manual PID tracking — complex, error-prone
4. Containers/cgroups — operational overhead

**Decision**: Use process groups.

**Rationale**:

- Handles grandchildren without needing to know if docling spawns subprocesses
- Well-documented Unix pattern recognizable to engineers
- Works for both graceful termination and forceful kill
- Minimal code change: add `os.setpgrp()` in subprocess, use `os.killpg()` in parent

**Consequences**:

- Linux/macOS only (no Windows support)
- Requires careful error handling for ESRCH (process group already gone)

### ADR-002: DB Polling for Cancellation

**Context**: Need to detect user-initiated job cancellation and stop processing within 5 seconds.

**Options**:

1. DB polling (current) — check every 2 seconds
2. Shared `mp.Event` — near-instant, requires passing event to child
3. Signal (SIGUSR1) — instant, requires signal handler in child
4. File/socket watch — complex, unnecessary

**Decision**: Keep DB polling with 2-second interval.

**Rationale**:

- Meets latency requirement (0-5 seconds acceptable)
- Simple: no IPC mechanism, no signal handlers
- Child checks at phase boundaries via `_raise_if_cancelled()`
- Parent can forcefully terminate as backstop

**Consequences**:

- Up to 2-second latency for cancellation detection
- Child must reach a checkpoint to observe cancellation cooperatively
- Forceful termination (SIGTERM/SIGKILL) always available

### ADR-003: Wall-Clock Timeout

**Context**: Need to prevent jobs from running indefinitely.

**Options**:

1. Wall-clock timeout — total time including waits/retries
2. Per-phase timeouts — separate limits for conversion, upload
3. Active-time-only timeout — exclude retry delays

**Decision**: Single wall-clock timeout (7200 seconds).

**Rationale**:

- Easier to reason about and configure
- Prevents jobs from consuming resources indefinitely even during retries
- Simpler implementation: single deadline, single check

**Consequences**:

- Upload retries with exponential backoff count against timeout
- Cannot distinguish "conversion took too long" from "upload retries exhausted time"
- Future enhancement: if needed, add per-phase limits later

### ADR-004: Exception Retryability Attribute

**Context**: Need to classify errors as retryable or permanent for `handle_job_error()`.

**Options**:

1. String set lookup (current) — `permanent_errors = {"error_code_1", ...}`
2. Exception class hierarchy — `PermanentError(ConversionError)`
3. Exception attribute — `error.retryable = True/False`
4. Protocol/ABC — abstract method `is_retryable()`

**Decision**: Add `retryable` boolean attribute to exception base class.

**Rationale**:

- Low migration effort: add attribute to existing classes incrementally
- Self-documenting: error type declares its own retry semantics
- Flexible: can override per-instance if needed
- No string matching: behavior encoded in type

**Consequences**:

- Requires updating exception classes to set `retryable` attribute
- `handle_job_error()` uses `getattr(error, "retryable", True)` for backward compatibility
- Clear contract: new exceptions must declare `retryable` explicitly

---

## Documentation Structure

```text
specs/002-worker-process-management/
├── spec.md              # Feature specification (this exists)
├── plan.md              # Implementation plan (this file)
└── tasks.md             # Implementation tasks (to be generated)
```

### Affected Source Files

```text
src/aizk/conversion/
├── workers/
│   ├── worker.py        # Main changes: process groups, exception attributes
│   ├── converter.py     # Update exceptions to include retryable attribute
│   └── fetcher.py       # Update exceptions to include retryable attribute
└── utilities/
    └── config.py        # Already has worker_job_timeout_seconds (staged)

tests/conversion/
├── unit/
│   └── test_worker.py   # Update tests for new behavior
├── integration/
│   ├── test_conversion_flow.py       # Update tests for subprocess behavior
│   ├── test_worker_concurrency.py    # Verify process group management
│   └── test_worker_lifecycle.py      # NEW: real subprocess tests
└── conftest.py          # Fixtures for subprocess mocking and real process testing
```

---

## Complexity Tracking

All Constitution Check gates passed. The following design decisions add necessary complexity:

1. **Process group management** (ADR-001):

   - **Why**: Handles grandchild processes from docling without requiring knowledge of its internals
   - **Alternative considered**: Manual PID tracking (rejected: error-prone, complex)
   - **Justification**: Standard Unix pattern, minimal code, defensive against unknown subprocess behavior

2. **Exception attribute for retryability** (ADR-004):

   - **Why**: Replaces fragile string-based error classification with type-safe approach
   - **Alternative considered**: Exception class hierarchy (rejected: more invasive refactor)
   - **Justification**: Low migration effort, self-documenting, backward compatible via `getattr`

**Gate Summary**:

- ✅ G1: Data provenance maintained (no new data storage)
- ✅ G2: Reproducibility preserved (deterministic behavior)
- ✅ G3: Test-first approach (unit + integration tests planned)
- ✅ G4: Privacy & safety (no PII, standard OS facilities)
- ✅ G5: Observability (structured logging, metrics)
- ✅ G6: ADRs documented (4 decisions above)
- ✅ G7: Governance (changelog, semver, conventional commits)
- ✅ G8: Preconditions met

---

## Implementation Readiness

**Phase 0 (Research)**: ✅ Complete

- Process group management pattern researched (standard POSIX)
- Signal handling options evaluated (DB polling sufficient)
- Timeout patterns evaluated (wall-clock vs. per-phase)
- Exception classification patterns evaluated (attribute vs. hierarchy)
- Test strategy defined (unit with mocks, integration with real processes)

**Phase 1 (Design)**: ✅ Complete

- Parent/child responsibility boundary clarified (see spec Functional Requirements)
- Error taxonomy refined (retryable attribute pattern)
- Phase list canonicalized: `starting`, `preparing_input`, `converting`, `uploading`
- Process termination sequence defined: SIGTERM → wait 5s → SIGKILL → wait 5s

**Phase 2 (Implementation)**: ⏳ Ready

Implementation will be broken into 5 incremental tasks:

### Task 1: Add Process Group Management

**Scope**: Modify subprocess startup to create process groups and termination to kill entire group

**Changes**:

- Add `os.setpgrp()` call at start of `_process_job_subprocess()`
- Update termination logic to use `os.killpg(process.pid, signal.SIGTERM/SIGKILL)`
- Handle `ProcessLookupError` (ESRCH) when process group already gone

**Tests**:

- Unit test: Mock process group behavior
- Integration test: Spawn subprocess that creates grandchild, verify both killed

**Acceptance**: No zombie processes remain after termination, including grandchildren

---

### Task 2: Add Exception Retryability Attribute

**Scope**: Refactor error classification from string lookup to exception attribute

**Changes**:

- Add `retryable` attribute to base `ConversionError` class (default `True`)
- Update `handle_job_error()` to use `getattr(error, "retryable", True)`
- Mark permanent errors: `JobDataIntegrityError.retryable = False`, `BookmarkContentError.retryable = False`, etc.
- Remove `permanent_errors` string set

**Tests**:

- Unit test: Verify retryable exceptions → FAILED_RETRYABLE
- Unit test: Verify permanent exceptions → FAILED_PERM
- Unit test: Verify backward compatibility (exceptions without attribute default to retryable)

**Acceptance**: All errors classified correctly, no string matching

---

### Task 3: Harden Cancellation Detection

**Scope**: Ensure cancellation is detected at all critical points

**Changes**:

- Verify `_raise_if_cancelled()` calls before/after conversion phase
- Add cancellation check before upload phase (already exists in staged code)
- Ensure parent logs "Job X cancelled during Y" with phase name

**Tests**:

- Unit test: Cancel during each phase, verify detection
- Integration test: Cancel mid-conversion, verify subprocess terminated within 5s

**Acceptance**: 95% of cancellations complete within 5 seconds

---

### Task 4: Verify Timeout Enforcement

**Scope**: Ensure timeout is checked at all phases and deadline is respected

**Changes**:

- Verify deadline computed as `time.monotonic() + timeout_seconds` after RUNNING transition
- Ensure timeout checked: during subprocess polling, before upload, during upload retries
- Verify `ConversionTimeoutError(phase=last_phase)` captures interrupted phase

**Tests**:

- Unit test: Mock timeout during each phase, verify termination
- Integration test: Real subprocess with short timeout, verify killed at deadline

**Acceptance**: Jobs exceeding timeout are terminated within ±10 seconds

---

### Task 5: Add Integration Tests for Real Subprocesses

**Scope**: Test actual subprocess behavior to complement unit test mocks

**Changes**:

- Add `test_worker_lifecycle.py` with real subprocess tests
- Test: Spawn simple subprocess, terminate via SIGTERM, verify exit
- Test: Spawn subprocess that creates grandchild, kill process group, verify both gone
- Test: Spawn subprocess, let it complete normally, verify temp directory cleaned
- Test: Simulate worker crash (don't clean temp dir), verify stale job recovery marks FAILED_RETRYABLE

**Tests**:

- All tests spawn real processes (no mocks)
- Use simple test targets (not real docling) for speed
- Verify process table after termination (no zombies)
- Verify filesystem after termination (no temp dirs)

**Acceptance**: All integration tests pass, verifying real process management

---

## Constitution Re-Check Post-Implementation

After implementation, verify:

- ✅ All tests pass (unit + integration)
- ✅ No zombie processes in process table after 100 job runs
- ✅ No temp directories in `/tmp` after 100 job runs (excluding active jobs)
- ✅ Cancellation latency measured: 95% within 5 seconds
- ✅ Timeout accuracy measured: ±10 seconds of configured deadline
- ✅ Error classification verified: retryable/permanent correctly assigned
- ✅ Phase transitions logged for all jobs
- ✅ CHANGELOG.md updated with improvements

---

## Next Steps

1. **Review plan**: Confirm design decisions (ADRs) and implementation tasks align with requirements

2. **Generate tasks.md**: Break down implementation into atomic, testable tasks

3. **Implement incrementally**:

   - Task 1: Process groups (most critical for cleanup)
   - Task 2: Exception attributes (improves error handling)
   - Task 3: Cancellation (hardens existing behavior)
   - Task 4: Timeout (hardens existing behavior)
   - Task 5: Integration tests (validates real behavior)

4. **Measure success criteria**:

   - Run 100-job test suite
   - Verify no zombies, no temp leaks
   - Measure cancellation/timeout latency
   - Update CHANGELOG.md

5. **Merge**: After all tests pass and success criteria met

---

**Planning Complete**: 2026-01-10\
**Branch**: feature/ingest (in-progress refactor)\
**Status**: Ready for implementation

**Key Improvements**:

- ✅ Process group management for grandchild cleanup
- ✅ Exception retryability attribute for type-safe error handling
- ✅ Hardened cancellation and timeout enforcement
- ✅ Comprehensive integration tests with real subprocesses
- ✅ Clear ADRs documenting design decisions
