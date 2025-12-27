# Worker Module Decomposition Design

**Date:** 2026-03-28

## Goal

Split `workers/worker.py` (988 lines, 15+ responsibilities) into focused modules with single responsibilities and independent test surfaces.
Pure structural refactor — no behavior change.

## Constraints

- Every function body stays identical.
  Move only, no logic changes.
- All existing tests pass with only import path updates.
- Follows the refactor guideline: "move/rename first, then change logic."
- No circular dependencies between new modules.
- Cross-module data sharing uses frozen dataclasses (immutable DTOs).

## Module Structure

```text
workers/
├── __init__.py          (unchanged)
├── converter.py         (existing, untouched)
├── fetcher.py           (existing, untouched)
├── errors.py            (NEW)
├── types.py             (NEW)
├── supervision.py       (NEW)
├── uploader.py          (NEW)
├── orchestrator.py      (NEW)
├── loop.py              (NEW)
└── worker.py            (temporary re-export shim, then deleted)
```

## Module Responsibilities

### `errors.py` — Worker exception classes

All exception classes currently defined in `worker.py`:

- `ConversionArtifactsMissingError`
- `ConversionCancelledError`
- `ConversionTimeoutError`
- `ConversionSubprocessError`
- `JobDataIntegrityError`
- `ReportedChildError`
- `PreflightError`

These are imported by multiple new modules (`supervision.py`, `orchestrator.py`, `uploader.py`), so they need a shared leaf module to avoid circular imports.

### `types.py` — Shared data types

Frozen dataclasses used across module boundaries:

- `ConversionInput` — source bytes + pipeline type + fetch timestamp
- `ConversionArtifacts` — local artifacts post-conversion (markdown path, figures, hash, pipeline name, docling version)
- `SupervisionResult` — subprocess supervision outcome (last phase, reported error, cancelled/timed out flags)

Leaf module with no internal imports beyond standard library and `pathlib`.

### `supervision.py` — Subprocess supervision (parent-side only)

Functions that monitor and control the child process from the parent:

- `_get_parent_pgid()` — retrieve parent process group ID
- `_terminate_child_process()` — SIGTERM/SIGKILL with process group awareness
- `_collect_status_messages()` — drain status queue non-blocking
- `_supervise_conversion_process()` — parent supervision loop (cancellation polling, timeout enforcement, status collection)

Imports from: `errors`, `types`.
No dependency on `orchestrator`.

Note: `_supervise_conversion_process` currently calls `handle_job_error` on timeout inline (lines 699–706).
To avoid a circular dependency (`orchestrator → supervision` for the supervise call, `supervision → orchestrator` for handle_job_error), this inline call is removed.
The function already returns `SupervisionResult(timed_out=True)` and the caller (`process_job_supervised` in `orchestrator.py`) already checks `result.timed_out`.
The orchestrator handles the timeout error instead — matching the existing pattern for cancellation.

**This is the single behavior-adjacent change in the refactor:** moving the timeout error-handling call site from `_supervise_conversion_process` to `process_job_supervised`.
The error is still handled identically; only the call site moves.

### `orchestrator.py` — Per-job orchestration (the shell)

The main per-job flow, child-process functions, and subprocess spawning:

**Child-process functions** (must be in the same module because `mp.Process` with spawn context pickles the target function and its references):

- `_report_status()` — send structured event from subprocess to parent via `mp.Queue`
- `_process_job_subprocess()` — subprocess entrypoint; wraps `_convert_job_artifacts` with error reporting
- `_convert_job_artifacts()` — child-process coordinator: load DB state, deserialize KaraKeep payload, prepare input, run conversion
- `_prepare_conversion_input()` — route to fetch strategy based on source/content type
- `_run_conversion()` — child-process conversion: call converter, normalize whitespace, write artifacts, compute hash

**Subprocess spawning** (references `_process_job_subprocess` as the target):

- `_spawn_conversion_subprocess()` — create spawn-context process with status queue

**Parent-process orchestration:**

- `_utcnow()` — timezone-aware UTC timestamp
- `_docling_version()` — installed docling version for metadata
- `_raise_if_cancelled()` — raise if job status is CANCELLED (used in child process)
- `_is_job_cancelled()` — poll-safe cancellation check (used in parent supervision)
- `_prepare_bookmark_for_job()` — parent-process preflight: fetch KaraKeep bookmark, validate, detect types, update DB records
- `_initialize_running_job()` — transition job to RUNNING state
- `process_job_supervised()` — primary entry point: preflight → spawn → supervise → upload → error handling
- `handle_job_error()` — persist failure details, compute retryability, schedule retry with exponential backoff

Imports from: `errors`, `types`, `supervision` (supervise only), `uploader` (upload).

### `uploader.py` — S3 artifact upload and output records

Single function:

- `_upload_converted()` — read metadata, check hash dedup, upload artifacts to S3, generate manifest, create `ConversionOutput` record, update job status to `SUCCEEDED`

Self-contained: takes job ID, workspace path, and config.
Does all DB + S3 work internally.

Imports from: `errors` (for `ConversionArtifactsMissingError`).

### `loop.py` — Worker polling loop

The outer event loop:

- `poll_and_process_jobs()` — `BEGIN IMMEDIATE` transaction to select next eligible job, transition to RUNNING, call `process_job_supervised`
- `recover_stale_running_jobs()` — find RUNNING jobs older than threshold, mark as `FAILED_RETRYABLE`
- `run_worker()` — infinite loop: periodic recovery + poll + sleep

Imports from: `orchestrator` (for `process_job_supervised`).

This is the only module `cli.py` needs to import from.

### `worker.py` — Temporary re-export shim

During the transition, `worker.py` re-exports all public names from the new modules so that existing imports (`cli.py` importing `run_worker`, tests importing `ConversionInput`) continue to work without a coordinated change.

The shim is removed in a final cleanup step after all external imports are updated to point at their new homes.

## Dependency Graph

```text
loop.py → orchestrator.py → supervision.py → errors.py, types.py
                           → uploader.py    → errors.py
                           → errors.py, types.py
```

No circular dependencies. `errors.py` and `types.py` are leaf modules.

## External Consumers

Only two external import sites exist:

| Consumer        | Current Import                                               | New Import                                                  |
| --------------- | ------------------------------------------------------------ | ----------------------------------------------------------- |
| `cli.py:67`     | `from aizk.conversion.workers.worker import run_worker`      | `from aizk.conversion.workers.loop import run_worker`       |
| Tests (3 files) | `from aizk.conversion.workers.worker import ConversionInput` | `from aizk.conversion.workers.types import ConversionInput` |

Both work unchanged via the re-export shim during transition.

## What Does Not Change

- No behavior changes to any function.
- `converter.py` and `fetcher.py` are untouched.
- All DB interactions, error handling, retry logic, subprocess management remain identical.
- The only call-site relocation is the timeout `handle_job_error` from inside `_supervise_conversion_process` to the `result.timed_out` branch in `process_job_supervised` (already exists, currently redundant).
