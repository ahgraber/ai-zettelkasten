# Config Singleton Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Parse `ConversionConfig` once at process startup and thread it through the system, eliminating repeated `.env` parsing and enabling config injection in tests.

**Architecture:** Each process entry point (`_cmd_serve`, `_cmd_worker`, `_cmd_db_init`) creates one `ConversionConfig` instance.
The API stores it on `app.state` and exposes it via FastAPI dependency injection.
The worker passes it as a parameter through its call chain.
The subprocess boundary in `_convert_job_artifacts` keeps its own instantiation (separate process).

**Tech Stack:** Python, pydantic-settings, FastAPI dependency injection, pytest

---

## Task 1: Add `get_config` FastAPI dependency and wire API layer

**Files:**

- Modify: `src/aizk/conversion/api/main.py`

- Modify: `src/aizk/conversion/api/dependencies.py`

- Modify: `src/aizk/conversion/api/routes/jobs.py`

- [ ] **Step 1: Run existing tests to establish baseline**

Run: `uv run pytest tests/conversion/contract/ tests/conversion/integration/test_job_status_counts.py tests/conversion/integration/test_jobs_actions.py -v`

Expected: All PASS

- [ ] **Step 2: Update `main.py` lifespan to store config on `app.state`**

In `src/aizk/conversion/api/main.py`, change the lifespan to store config on `_app.state`:

```python
@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Initialize resources needed for the API lifespan."""
    config = ConversionConfig()
    _app.state.config = config
    configure_logging(config)
    configure_mlflow_tracing(
        enabled=config.mlflow_tracing_enabled,
        tracking_uri=config.mlflow_tracking_uri,
        experiment_name=config.mlflow_experiment_name,
    )
    create_db_and_tables()
    yield
```

- [ ] **Step 3: Add `get_config` dependency and update `get_s3_client` in `dependencies.py`**

Replace `src/aizk/conversion/api/dependencies.py` contents:

```python
"""FastAPI dependencies for database sessions and S3 clients."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Request
from sqlmodel import Session

from aizk.conversion.db import get_session
from aizk.conversion.storage.s3_client import S3Client
from aizk.conversion.utilities.config import ConversionConfig


def get_config(request: Request) -> ConversionConfig:
    """Return the shared config instance from application state."""
    return request.app.state.config


def get_db_session() -> Iterator[Session]:
    """Provide a database session for request handling."""
    yield from get_session()


def get_s3_client(request: Request) -> S3Client:
    """Provide an S3Client configured from application state."""
    return S3Client(get_config(request))
```

- [ ] **Step 4: Update `submit_job` in `jobs.py` to accept config via dependency**

In `src/aizk/conversion/api/routes/jobs.py`, add the import and update the route:

Add `Request` import and update the `submit_job` function signature:

```python
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
```

Remove the `ConversionConfig` import:

```python
# DELETE: from aizk.conversion.utilities.config import ConversionConfig
```

Add `get_config` to the dependency imports:

```python
from aizk.conversion.api.dependencies import get_config, get_db_session
```

Update `submit_job` to receive config via dependency:

```python
@router.post("", response_model=JobResponse, status_code=status.HTTP_201_CREATED)
def submit_job(
    submission: JobSubmission,
    api_response: Response,
    session: Annotated[Session, Depends(get_db_session)],
    request: Request,
) -> JobResponse:
    """Submit a new conversion job."""
    config = get_config(request)
    # ... rest of function unchanged
```

- [ ] **Step 5: Run tests to verify API layer still works**

Run: `uv run pytest tests/conversion/contract/ tests/conversion/integration/test_job_status_counts.py tests/conversion/integration/test_jobs_actions.py -v`

Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add src/aizk/conversion/api/main.py src/aizk/conversion/api/dependencies.py src/aizk/conversion/api/routes/jobs.py
git commit -S -m "refactor(conversion/api): thread config via FastAPI dependency injection

Store ConversionConfig on app.state during lifespan and expose via
get_config dependency. Eliminates per-request config parsing in
submit_job and get_s3_client."
```

---

### Task 2: Thread config through worker parent-process functions

**Files:**

- Modify: `src/aizk/conversion/workers/worker.py`
- Modify: `src/aizk/conversion/cli.py`

These functions in `worker.py` currently create their own `ConversionConfig()` and need to accept it as a parameter instead: `run_worker`, `poll_and_process_jobs`, `process_job_supervised`, `handle_job_error`, `_upload_converted`.

The subprocess function `_convert_job_artifacts` keeps its own `ConversionConfig()` — it runs in a separate spawned process.

- [ ] **Step 1: Run existing worker tests to establish baseline**

Run: `uv run pytest tests/conversion/unit/test_worker.py -v`

Expected: All PASS

- [ ] **Step 2: Update `_upload_converted` to accept config**

In `src/aizk/conversion/workers/worker.py`, change the signature from:

```python
def _upload_converted(job_id: int, workspace: Path) -> None:
    """Upload artifacts to S3 and record conversion output in the DB."""
    config = ConversionConfig()
    engine = get_engine(config.database_url)
```

to:

```python
def _upload_converted(job_id: int, workspace: Path, config: ConversionConfig) -> None:
    """Upload artifacts to S3 and record conversion output in the DB."""
    engine = get_engine(config.database_url)
```

- [ ] **Step 3: Update `handle_job_error` to accept config**

Change the signature from:

```python
def handle_job_error(job_id: int, error: Exception) -> None:
    """Persist job failure details and compute retryability.

    Retry decision uses the `retryable` class attribute on every exception class.
    """
    config = ConversionConfig()
    engine = get_engine(config.database_url)
```

to:

```python
def handle_job_error(job_id: int, error: Exception, config: ConversionConfig) -> None:
    """Persist job failure details and compute retryability.

    Retry decision uses the `retryable` class attribute on every exception class.
    """
    engine = get_engine(config.database_url)
```

- [ ] **Step 4: Update `process_job_supervised` to accept config and pass it through**

Change the signature from:

```python
def process_job_supervised(job_id: int, poll_interval_seconds: float = 2.0) -> None:
    """Run a supervised conversion attempt and upload artifacts on success.

    The parent process handles preflight, cancellation, timeout, and uploads.
    """
    config = ConversionConfig()
    engine = get_engine(config.database_url)
```

to:

```python
def process_job_supervised(job_id: int, config: ConversionConfig, poll_interval_seconds: float = 2.0) -> None:
    """Run a supervised conversion attempt and upload artifacts on success.

    The parent process handles preflight, cancellation, timeout, and uploads.
    """
    engine = get_engine(config.database_url)
```

Then update all calls to `handle_job_error` within `process_job_supervised` to pass `config`:

```python
handle_job_error(job_id, exc, config)
```

There are 5 `handle_job_error` calls in this function — update all of them.

Update the call to `_upload_converted` to pass `config`:

```python
_upload_converted(job_id, workspace, config)
```

- [ ] **Step 5: Update `poll_and_process_jobs` to accept config and pass it through**

Change the signature from:

```python
def poll_and_process_jobs(poll_interval_seconds: float = 2.0) -> bool:
    """Pick up the next eligible job and invoke supervised processing."""
    config = ConversionConfig()
    engine = get_engine(config.database_url)
```

to:

```python
def poll_and_process_jobs(config: ConversionConfig, poll_interval_seconds: float = 2.0) -> bool:
    """Pick up the next eligible job and invoke supervised processing."""
    engine = get_engine(config.database_url)
```

Update the call to `process_job_supervised`:

```python
process_job_supervised(job_id, config, poll_interval_seconds=poll_interval_seconds)
```

- [ ] **Step 6: Update `run_worker` to accept config and pass it through**

Change the signature from:

```python
def run_worker(poll_interval_seconds: float = 2.0) -> None:
    """Run the worker loop for polling, processing, and recovery."""
    logger.info("Starting conversion worker loop")
    config = ConversionConfig()
```

to:

```python
def run_worker(config: ConversionConfig, poll_interval_seconds: float = 2.0) -> None:
    """Run the worker loop for polling, processing, and recovery."""
    logger.info("Starting conversion worker loop")
```

Update the call to `poll_and_process_jobs`:

```python
processed = poll_and_process_jobs(config, poll_interval_seconds=poll_interval_seconds)
```

- [ ] **Step 7: Update `_cmd_worker` in `cli.py` to pass config**

In `src/aizk/conversion/cli.py`, change:

```python
    run_worker()
```

to:

```python
    run_worker(config)
```

- [ ] **Step 8: Remove unused `ConversionConfig` import from `worker.py`**

The import `from aizk.conversion.utilities.config import ConversionConfig` is still needed for the type annotation in function signatures and for `_convert_job_artifacts`.
Keep it.

Verify no remaining `ConversionConfig()` calls exist in parent-process functions (only `_convert_job_artifacts` should have one).

- [ ] **Step 9: Run worker tests (expect failures from signature changes)**

Run: `uv run pytest tests/conversion/unit/test_worker.py -v`

Expected: Some tests FAIL because they call `process_job_supervised(job.id)` without the new `config` parameter, and monkeypatch `handle_job_error` / `_upload_converted` with the old arity.

- [ ] **Step 10: Fix unit tests for new signatures**

In `tests/conversion/unit/test_worker.py`:

**Remove `_FakeConfig` class and its monkeypatching.**
Instead, create a real config fixture and pass it directly.

Add a fixture near the top of the file:

```python
@pytest.fixture()
def worker_config():
    """Provide a ConversionConfig for worker tests."""
    return ConversionConfig(_env_file=None)
```

Add the import:

```python
from aizk.conversion.utilities.config import ConversionConfig
```

**Update test functions that call `process_job_supervised`:**

For tests that previously did `monkeypatch.setattr(worker, "ConversionConfig", lambda: _FakeConfig())`:

- Remove that monkeypatch line
- Create config directly: `config = ConversionConfig(_env_file=None, worker_job_timeout_seconds=1, retry_max_attempts=2, retry_base_delay_seconds=0)`
- Pass config to the call: `worker.process_job_supervised(job.id, config, poll_interval_seconds=...)`

For tests that monkeypatch `_upload_converted` or `handle_job_error`:

- Update lambda arity to match new signatures:

  - `lambda _job_id, _workspace:` → `lambda _job_id, _workspace, _config:`
  - `lambda _job_id, error:` → `lambda _job_id, error, _config:`
  - `lambda _job_id, _error:` → `lambda _job_id, _error, _config:`

- [ ] **Step 11: Run all worker tests**

Run: `uv run pytest tests/conversion/unit/test_worker.py -v`

Expected: All PASS

- [ ] **Step 12: Run full test suite**

Run: `uv run pytest tests/conversion/ -v`

Expected: All PASS (integration tests that call worker functions may also need updates — fix any that fail by passing config)

- [ ] **Step 13: Commit**

```bash
git add src/aizk/conversion/workers/worker.py src/aizk/conversion/cli.py tests/conversion/unit/test_worker.py
git commit -S -m "refactor(conversion/worker): thread config through worker call chain

run_worker, poll_and_process_jobs, process_job_supervised,
handle_job_error, and _upload_converted now accept config as a
parameter. Subprocess boundary (_convert_job_artifacts) retains its
own instantiation. Tests pass config directly instead of
monkeypatching ConversionConfig."
```

---

### Task 3: Remove config fallback from `db.py` and `logging.py`

**Files:**

- Modify: `src/aizk/conversion/db.py`

- Modify: `src/aizk/conversion/utilities/logging.py`

- Modify: `src/aizk/conversion/cli.py`

- [ ] **Step 1: Run existing tests to establish baseline**

Run: `uv run pytest tests/conversion/ -v`

Expected: All PASS

- [ ] **Step 2: Make `database_url` required in `get_engine`**

In `src/aizk/conversion/db.py`, change:

```python
def get_engine(database_url: str | None = None) -> Engine:
    """Create a database engine with SQLite tuning when applicable."""
    if database_url is None:
        config = ConversionConfig()
        database_url = config.database_url
```

to:

```python
def get_engine(database_url: str) -> Engine:
    """Create a database engine with SQLite tuning when applicable."""
```

Remove the `ConversionConfig` import from `db.py`.

- [ ] **Step 3: Update `_cmd_db_init` in `cli.py` to pass database_url**

In `src/aizk/conversion/cli.py`, change:

```python
def _cmd_db_init(_args: argparse.Namespace) -> int:
    """Initialize database tables."""
    setproctitle("docling-db-init")
    create_db_and_tables()
    return 0
```

to:

```python
def _cmd_db_init(_args: argparse.Namespace) -> int:
    """Initialize database tables."""
    setproctitle("docling-db-init")
    config = ConversionConfig()
    create_db_and_tables(get_engine(config.database_url))
    return 0
```

Add the `get_engine` import to `cli.py`:

```python
from aizk.conversion.db import create_db_and_tables, get_engine
```

- [ ] **Step 4: Make config required in `configure_logging`**

In `src/aizk/conversion/utilities/logging.py`, change:

```python
def configure_logging(config: ConversionConfig | None = None) -> None:
    """Configure logging for the conversion service."""
    config = config or ConversionConfig()
```

to:

```python
def configure_logging(config: ConversionConfig) -> None:
    """Configure logging for the conversion service."""
```

- [ ] **Step 5: Run full test suite and fix any failures**

Run: `uv run pytest tests/conversion/ -v`

The `set_test_env` autouse fixture in `conftest.py` sets `DATABASE_URL`, and test fixtures call `get_engine(f"sqlite:///{test_db_path}")` with an explicit URL.
Tests calling `get_engine()` with no args will fail — fix by passing the URL explicitly.

Expected: All PASS (after fixing any callers)

- [ ] **Step 6: Commit**

```bash
git add src/aizk/conversion/db.py src/aizk/conversion/utilities/logging.py src/aizk/conversion/cli.py
git commit -S -m "refactor(conversion): remove config fallbacks from db and logging

get_engine now requires database_url. configure_logging now requires
config. Eliminates hidden ConversionConfig instantiation in library
code."
```

---

### Task 4: Verify no stray `ConversionConfig()` calls remain in parent-process code

**Files:** None expected — verification only.

- [ ] **Step 1: Search for remaining `ConversionConfig()` calls**

Run: `grep -rn 'ConversionConfig()' src/aizk/conversion/`

Expected remaining calls:

- `src/aizk/conversion/workers/worker.py` in `_convert_job_artifacts` (subprocess boundary — intentional)

Any other hits indicate missed refactoring — fix them.

- [ ] **Step 2: Run full test suite**

Run: `uv run pytest tests/conversion/ -v`

Expected: All PASS

- [ ] **Step 3: Commit (if any fixes were needed)**

Only if step 1 found stray calls that needed fixing.
