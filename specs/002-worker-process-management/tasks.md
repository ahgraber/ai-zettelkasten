# Tasks: Robust Worker Process Management

**Input**: Design documents from `/specs/002-worker-process-management/`
**Prerequisites**: spec.md, plan.md

**Context**: This refactors the existing worker to harden process management, cancellation, timeout, and cleanup. The current staged changes already provide subprocess isolation, cancellation polling, timeout checking, and phase reporting—these tasks refine and strengthen that foundation.

**Constitution Alignment**: Tasks ensure reliable resource cleanup (no zombies, no temp leaks), observable operations (phase logging), and reproducible error handling (retryability attributes).

**Organization**: Tasks are grouped by implementation focus area (process groups, error handling, hardening, testing) to enable incremental delivery.

## Format: `[ID] [P?] [Focus] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Focus]**: Implementation area (e.g., ProcessGroups, ErrorHandling, Cancellation, Timeout, Testing)
- Include exact file paths in descriptions

---

## Phase 1: Foundation - Process Group Management

**Purpose**: Enable termination of entire process trees to prevent zombie processes

**⚠️ CRITICAL**: This is the most important robustness improvement

### Tests for Process Groups (write first)

- [x] T001 [P] [ProcessGroups] Unit test for process group creation in tests/conversion/unit/test_worker.py: mock `os.setpgrp()` call, verify it's invoked at subprocess start
- [x] T002 [P] [ProcessGroups] Unit test for process group termination in tests/conversion/unit/test_worker.py: mock `os.killpg()` calls, verify SIGTERM followed by SIGKILL with correct PID and signal numbers
- [x] T003 [P] [ProcessGroups] Unit test for ProcessLookupError handling in tests/conversion/unit/test_worker.py: mock `os.killpg()` to raise ESRCH, verify exception is caught and logged but doesn't propagate
- [x] T004 [ProcessGroups] Integration test for real process group termination in tests/conversion/integration/test_worker_lifecycle.py: spawn subprocess that creates grandchild (via `subprocess.Popen`), call termination logic, verify both parent and grandchild processes are killed (check process table)
- [x] T005 [ProcessGroups] Obtain user approval of process group tests and confirm red phase before implementation

### Implementation for Process Groups

- [x] T006 [ProcessGroups] Add `os.setpgrp()` call at start of `_process_job_subprocess()` in src/aizk/conversion/workers/worker.py to create new process group
- [x] T007 [ProcessGroups] Import `signal` and `os` modules in src/aizk/conversion/workers/worker.py for process group management
- [x] T008 [ProcessGroups] Refactor termination in `process_job_supervised()` in src/aizk/conversion/workers/worker.py: replace `process.terminate()` with `os.killpg(process.pid, signal.SIGTERM)` for graceful termination
- [x] T009 [ProcessGroups] Refactor forceful kill in `process_job_supervised()` in src/aizk/conversion/workers/worker.py: replace `process.kill()` with `os.killpg(process.pid, signal.SIGKILL)` for forceful termination
- [x] T010 [ProcessGroups] Add try/except block around `os.killpg()` calls in src/aizk/conversion/workers/worker.py to catch and log `ProcessLookupError` (errno ESRCH) when process group already gone
- [x] T011 [ProcessGroups] Add structured log message in src/aizk/conversion/workers/worker.py when process group is successfully terminated: "Terminated process group for job {job_id}"

**Checkpoint**: ✅ COMPLETE - Process groups implemented and tested - grandchild processes are now cleaned up reliably

---

## Phase 2: Error Handling - Retryability Attributes

**Purpose**: Replace fragile string-based error classification with type-safe exception attributes

### Tests for Error Retryability (write first)

- [ ] T012 [P] [ErrorHandling] Unit test for retryable exception in tests/conversion/unit/test_worker.py: create exception with `retryable=True`, call `handle_job_error()`, verify job transitions to FAILED_RETRYABLE with `earliest_next_attempt_at` set
- [ ] T013 [P] [ErrorHandling] Unit test for permanent exception in tests/conversion/unit/test_worker.py: create exception with `retryable=False`, call `handle_job_error()`, verify job transitions to FAILED_PERM with `finished_at` set and `earliest_next_attempt_at=None`
- [ ] T014 [P] [ErrorHandling] Unit test for backward compatibility in tests/conversion/unit/test_worker.py: create exception without `retryable` attribute, call `handle_job_error()`, verify defaults to retryable (FAILED_RETRYABLE)
- [ ] T015 [P] [ErrorHandling] Unit test for error code mapping in tests/conversion/unit/test_worker.py: verify each exception type has correct `error_code` and `retryable` attribute combination
- [ ] T016 [ErrorHandling] Obtain user approval of error handling tests and confirm red phase before implementation

### Implementation for Error Retryability

- [ ] T017 [P] [ErrorHandling] Add `retryable = True` class attribute to `ConversionError` base class in src/aizk/conversion/workers/converter.py (if exists) or create base exception class
- [ ] T018 [P] [ErrorHandling] Set `retryable = False` on `JobDataIntegrityError` class in src/aizk/conversion/workers/worker.py
- [ ] T019 [P] [ErrorHandling] Set `retryable = True` on `ConversionTimeoutError` class in src/aizk/conversion/workers/worker.py (timeouts are transient)
- [ ] T020 [P] [ErrorHandling] Set `retryable = True` on `ConversionSubprocessError` class in src/aizk/conversion/workers/worker.py (crashes may be transient)
- [ ] T021 [P] [ErrorHandling] Set `retryable = False` on `BookmarkContentError` class in src/aizk/conversion/utilities/bookmark_utils.py (missing content is permanent)
- [ ] T022 [P] [ErrorHandling] Set `retryable = True` on `FetchError` class in src/aizk/conversion/workers/fetcher.py (network errors are transient)
- [ ] T023 [P] [ErrorHandling] Set `retryable` attribute on other relevant exception classes in src/aizk/conversion/workers/converter.py (e.g., `DoclingEmptyOutputError` should be permanent)
- [ ] T024 [ErrorHandling] Update `handle_job_error()` in src/aizk/conversion/workers/worker.py: replace `permanent_errors` string set with `retryable = getattr(error, "retryable", True)`
- [ ] T025 [ErrorHandling] Remove `permanent_errors` string set from src/aizk/conversion/workers/worker.py
- [ ] T026 [P] [ErrorHandling] Add comment/docstring to base exception class in src/aizk/conversion/workers/converter.py documenting the `retryable` attribute contract

**Checkpoint**: Error classification now uses type-safe exception attributes

---

## Phase 3: Hardening - Cancellation Detection

**Purpose**: Ensure cancellation is detected at all critical checkpoints and logged with phase information

### Tests for Cancellation (write first)

- [ ] T027 [P] [Cancellation] Unit test for cancellation during preflight in tests/conversion/unit/test_worker.py: mark job CANCELLED before calling `process_job_supervised()`, verify early return without subprocess spawn
- [ ] T028 [P] [Cancellation] Unit test for cancellation during conversion in tests/conversion/unit/test_worker.py: mock subprocess alive, set job CANCELLED on first poll, verify subprocess terminated and job remains CANCELLED
- [ ] T029 [P] [Cancellation] Unit test for cancellation before upload in tests/conversion/unit/test_worker.py: mock successful subprocess completion, set job CANCELLED before upload phase, verify upload skipped and job remains CANCELLED
- [ ] T030 [P] [Cancellation] Unit test for cancellation logging in tests/conversion/unit/test_worker.py: verify log message includes "Job {id} cancelled during {phase}" format
- [ ] T031 [Cancellation] Integration test for cancellation latency in tests/conversion/integration/test_conversion_flow.py: submit job, cancel mid-execution, measure time until subprocess terminated, verify \<5 seconds
- [ ] T032 [Cancellation] Obtain user approval of cancellation tests and confirm red phase before implementation

### Implementation for Cancellation

- [ ] T033 [Cancellation] Verify `_raise_if_cancelled()` is called before `_prepare_conversion_input()` in src/aizk/conversion/workers/worker.py (already exists in staged code, verify placement)
- [ ] T034 [Cancellation] Verify `_raise_if_cancelled()` is called after `_run_conversion()` in src/aizk/conversion/workers/worker.py (already exists in staged code, verify placement)
- [ ] T035 [Cancellation] Verify cancellation check before upload phase in `process_job_supervised()` in src/aizk/conversion/workers/worker.py (already exists: `if _is_job_cancelled(...)` before upload)
- [ ] T036 [Cancellation] Add structured log message when cancellation detected during subprocess polling in src/aizk/conversion/workers/worker.py: "Job {job_id} cancelled during {last_phase}"
- [ ] T037 [Cancellation] Add structured log message when cancellation detected before upload in src/aizk/conversion/workers/worker.py: "Job {job_id} cancelled before upload"
- [ ] T038 [P] [Cancellation] Verify early return in `process_job_supervised()` for CANCELLED jobs in src/aizk/conversion/workers/worker.py (already exists: check `job.status in {SUCCEEDED, CANCELLED}`)

**Checkpoint**: Cancellation is now detected reliably with proper logging

---

## Phase 4: Hardening - Timeout Enforcement

**Purpose**: Ensure timeout is enforced across all phases including upload retries

### Tests for Timeout (write first)

- [ ] T039 [P] [Timeout] Unit test for timeout during subprocess in tests/conversion/unit/test_worker.py: mock `time.monotonic()` to exceed deadline during subprocess polling, verify subprocess terminated and `ConversionTimeoutError` raised with correct phase
- [ ] T040 [P] [Timeout] Unit test for timeout before upload in tests/conversion/unit/test_worker.py: mock deadline exceeded after subprocess completes but before upload, verify `ConversionTimeoutError` raised with phase="uploading"
- [ ] T041 [P] [Timeout] Unit test for timeout during upload retry in tests/conversion/unit/test_worker.py: mock deadline exceeded during retry loop, verify loop exits and `ConversionTimeoutError` raised
- [ ] T042 [P] [Timeout] Unit test for timeout phase capture in tests/conversion/unit/test_worker.py: verify `ConversionTimeoutError.phase` contains the last known phase from status queue
- [ ] T043 [Timeout] Integration test for real timeout in tests/conversion/integration/test_worker_lifecycle.py: configure short timeout (10s), spawn subprocess that sleeps >10s, verify killed at deadline ±2s
- [ ] T044 [Timeout] Obtain user approval of timeout tests and confirm red phase before implementation

### Implementation for Timeout

- [ ] T045 [Timeout] Verify deadline computation in `process_job_supervised()` in src/aizk/conversion/workers/worker.py: `deadline = time.monotonic() + timeout_seconds` after RUNNING transition (already exists in staged code)
- [ ] T046 [Timeout] Verify timeout check during subprocess polling loop in src/aizk/conversion/workers/worker.py: `if deadline and time.monotonic() >= deadline` (already exists in staged code)
- [ ] T047 [Timeout] Verify timeout check before upload phase in src/aizk/conversion/workers/worker.py: check deadline before entering `_upload_converted()` (already exists in staged code)
- [ ] T048 [Timeout] Verify timeout check during upload retry loop in src/aizk/conversion/workers/worker.py: check deadline at start of each retry iteration (already exists in staged code)
- [ ] T049 [Timeout] Verify `ConversionTimeoutError` includes `phase` parameter in src/aizk/conversion/workers/worker.py: `ConversionTimeoutError(message, phase=last_phase)` (already exists in staged code)
- [ ] T050 [P] [Timeout] Add structured log message when timeout detected in src/aizk/conversion/workers/worker.py: "Job {job_id} timed out during {phase} after {elapsed}s"

**Checkpoint**: Timeout is enforced consistently across all phases

---

## Phase 5: Validation - Integration Tests

**Purpose**: Verify real subprocess behavior to complement unit test mocks

### Integration Tests (write and implement together)

- [ ] T051 [Testing] Create tests/conversion/integration/test_worker_lifecycle.py with fixtures for simple subprocess targets (e.g., Python script that sleeps, script that spawns child, script that writes to temp file)
- [ ] T052 [P] [Testing] Integration test: spawn subprocess that completes normally in tests/conversion/integration/test_worker_lifecycle.py, verify exit code 0, temp directory cleaned, no processes remain
- [ ] T053 [P] [Testing] Integration test: spawn subprocess that spawns grandchild in tests/conversion/integration/test_worker_lifecycle.py, terminate via process group, verify both parent and grandchild killed (check `ps` or `/proc`)
- [ ] T054 [P] [Testing] Integration test: spawn subprocess, send SIGTERM, verify graceful shutdown within 5s in tests/conversion/integration/test_worker_lifecycle.py
- [ ] T055 [P] [Testing] Integration test: spawn subprocess that ignores SIGTERM, verify SIGKILL sent after 5s in tests/conversion/integration/test_worker_lifecycle.py
- [ ] T056 [P] [Testing] Integration test: spawn subprocess with short timeout (5s), let it run longer, verify killed at deadline in tests/conversion/integration/test_worker_lifecycle.py
- [ ] T057 [P] [Testing] Integration test: spawn subprocess, cancel job mid-execution, verify terminated within poll interval in tests/conversion/integration/test_worker_lifecycle.py
- [ ] T058 [P] [Testing] Integration test: simulate worker crash (don't call context manager cleanup), verify stale job recovery marks job FAILED_RETRYABLE in tests/conversion/integration/test_worker_lifecycle.py
- [ ] T059 [Testing] Add helper function to check process table for zombies in tests/conversion/integration/test_worker_lifecycle.py: `assert_no_zombie_processes(job_id)` using `psutil` or `ps` command
- [ ] T060 [Testing] Add helper function to check temp directory cleanup in tests/conversion/integration/test_worker_lifecycle.py: `assert_no_temp_directories(pattern)` checking `/tmp` or `tempfile.gettempdir()`

**Checkpoint**: Integration tests validate real process management behavior

---

## Phase 6: Documentation and Validation

**Purpose**: Update documentation and validate all success criteria

### Documentation

- [ ] T061 [P] [Docs] Update CHANGELOG.md with `fix(worker):` entries for process group management, exception retryability, hardened cancellation/timeout
- [ ] T062 [P] [Docs] Add docstring to `_process_job_subprocess()` in src/aizk/conversion/workers/worker.py explaining process group creation
- [ ] T063 [P] [Docs] Add docstring to `handle_job_error()` in src/aizk/conversion/workers/worker.py explaining retryability classification via exception attribute
- [ ] T064 [P] [Docs] Update AGENTS.md with process management principles if not already documented

### Validation

- [ ] T065 [Validation] Run 100-job test suite (mix of success/failure/cancel/timeout) and measure: cancellation latency (95th percentile \<5s), timeout accuracy (±10s), zombie process count (0), temp directory leaks (0)
- [ ] T066 [Validation] Verify all unit tests pass with >90% coverage for worker.py
- [ ] T067 [Validation] Verify all integration tests pass and demonstrate real subprocess behavior
- [ ] T068 [Validation] Manual test: submit long-running job, cancel via API, observe logs show "Job X cancelled during Y" within 5s
- [ ] T069 [Validation] Manual test: submit job that times out, observe error includes phase and job marked FAILED_RETRYABLE
- [ ] T070 [Validation] Run `ps aux | grep defunct` after 10 job executions to verify no zombie processes

**Checkpoint**: All success criteria met and documented

---

## Dependency Graph

```text
Phase 1 (Process Groups):     T001-T005 → T006-T011
                                     ↓
Phase 2 (Error Handling):     T012-T016 → T017-T026  (parallel with Phase 1)
                                     ↓
Phase 3 (Cancellation):       T027-T032 → T033-T038  (depends on Phase 1 termination changes)
                                     ↓
Phase 4 (Timeout):            T039-T044 → T045-T050  (parallel with Phase 3)
                                     ↓
Phase 5 (Integration Tests):  T051-T060              (depends on all implementation phases)
                                     ↓
Phase 6 (Docs & Validation):  T061-T070              (final validation)
```

---

## Parallel Execution Opportunities

**Can work in parallel after Phase 1 tests approved:**

- Phase 2 (Error Handling): T012-T026 (different exceptions, independent changes)
- Phase 1 (Process Groups): T006-T011 (subprocess termination changes)

**Can work in parallel after Phases 1-2 complete:**

- Phase 3 (Cancellation): T033-T038 (checkpoint verification)
- Phase 4 (Timeout): T045-T050 (deadline enforcement)

**Can work in parallel in Phase 5:**

- All integration tests (T052-T060) test different behaviors independently

---

## Implementation Strategy

1. **Start with Process Groups (Phase 1)** — most critical for cleanup guarantees
2. **Add Error Attributes (Phase 2)** — improves error handling immediately
3. **Harden Cancellation/Timeout (Phases 3-4)** — verify existing behavior is complete
4. **Validate with Integration Tests (Phase 5)** — prove real subprocess management works
5. **Document and Measure (Phase 6)** — verify success criteria and update changelog

---

## Success Criteria Validation

After completing all tasks, verify:

- ✅ **Cancellation latency**: Run T065, confirm 95% of cancellations \<5s
- ✅ **Timeout accuracy**: Run T065, confirm timeouts within ±10s of deadline
- ✅ **Zombie processes**: Run T070, confirm 0 zombies after 10+ jobs
- ✅ **Temp directory cleanup**: Run T065, confirm 0 leaked dirs after 100 jobs
- ✅ **Phase logging**: Grep logs for "entered phase", verify all jobs log all phases
- ✅ **Error classification**: Review test results, confirm retryable/permanent correct
- ✅ **Integration tests pass**: All real subprocess tests pass (T052-T060)

---

**Tasks Generated**: 2026-01-10
**Last Updated**: 2026-01-10 (Phase 1 tests passing)
**Total Tasks**: 70
**Completed**: 11/70 (Phase 1 complete)
**Status**: ✅ Phase 1 PASSED - All process group tests passing
**Branch**: feature/job-processes

**Task Breakdown**:

- Phase 1 (Process Groups): ✅ 11/11 COMPLETE (5 tests + 6 implementation)
- Phase 2 (Error Handling): ⏸️ 0/15 NOT STARTED (can proceed independently)
- Phase 3 (Cancellation): ⏸️ 0/12 NOT STARTED (can proceed independently)
- Phase 4 (Timeout): ⏸️ 0/12 NOT STARTED (can proceed independently)
- Phase 5 (Integration Tests): ⏸️ 1/10 PARTIAL (scaffold created)
- Phase 6 (Documentation & Validation): ⏸️ 0/10 NOT STARTED

**Recommended Next**: Phase 2 (Error Handling) - Replace string-based error codes with retryable exception attributes
