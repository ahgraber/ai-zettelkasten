# Tasks: worker-error-retryability

## Task 1: Add missing `retryable` class attributes ✓

**File:** `src/aizk/conversion/workers/worker.py`

- Add `retryable: ClassVar[bool] = False` to `ConversionArtifactsMissingError`
- Add `retryable: ClassVar[bool] = False` to `ConversionCancelledError`
- Add `retryable: ClassVar[bool] = True` to `ReportedChildError`
  (instance `self.retryable` override in `__init__` can remain for per-instance cases)

**Verification:** `grep -n "retryable" src/aizk/conversion/workers/worker.py` shows all exception
classes now carry the class-level attribute.

## Task 2: Remove `getattr` fallbacks ✓

**File:** `src/aizk/conversion/workers/worker.py`

- In `handle_job_error()` (~line 865): replace
  `retryable = getattr(error, "retryable", True)` with `retryable = error.retryable`
- In `_process_job_subprocess()` (~line 544): replace
  `retryable = getattr(exc, "retryable", True)` with `retryable = exc.retryable`

**Verification:** `grep -n "getattr.*retryable" src/aizk/conversion/workers/worker.py` returns no results.

## Task 3: Update unit tests ✓

**File:** `tests/conversion/unit/test_worker.py` (or equivalent)

- Assert `ConversionArtifactsMissingError.retryable is False`
- Assert `ConversionCancelledError.retryable is False`
- Assert `ReportedChildError.retryable is True` (class-level default)
- Assert `ReportedChildError("msg", "code", retryable=False).retryable is False`
  (instance override still works)
- Assert that `handle_job_error` with a `ConversionArtifactsMissingError` transitions job to
  `FAILED_PERM`

**Verification:** `uv run pytest tests/conversion/unit/test_worker.py -v` passes.
