# Worker Module Decomposition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the 988-line `workers/worker.py` into six focused modules with no behavior change.

**Architecture:** Pure structural refactor.
Code moves from `worker.py` into `errors.py`, `types.py`, `supervision.py`, `uploader.py`, `orchestrator.py`, and `loop.py`.
Two boundary refinements enable clean module separation: (1) timeout error handling moves from supervision to orchestrator, (2) cancellation checking becomes a callback to eliminate supervision's DB dependency.
`worker.py` becomes a temporary re-export shim, then imports are updated to final locations.

**Tech Stack:** Python 3.12, SQLModel, multiprocessing, pytest

**Python skills to follow:** `python-design-modularity` (module boundaries, refactor guidelines), `python-testing` (patch at import location used by unit under test).

---

## Task 1: Create `errors.py` and `types.py`

Leaf modules with no internal dependencies.
All exception classes and shared data types.

**Files:**

- Create: `src/aizk/conversion/workers/errors.py`

- Create: `src/aizk/conversion/workers/types.py`

- [ ] **Step 1: Create `errors.py`**

```python
"""Exception classes for the conversion worker."""

from __future__ import annotations

from typing import ClassVar


class ConversionArtifactsMissingError(RuntimeError):
    """Raised when expected conversion artifacts are missing."""

    error_code = "conversion_artifacts_missing"
    retryable: ClassVar[bool] = False


class ConversionCancelledError(RuntimeError):
    """Raised when a conversion job is cancelled during processing."""

    error_code = "conversion_cancelled"
    retryable: ClassVar[bool] = False


class ConversionTimeoutError(RuntimeError):
    """Raised when a conversion job exceeds the configured timeout."""

    error_code = "conversion_timeout"
    retryable: ClassVar[bool] = True

    def __init__(self, message: str, phase: str) -> None:
        super().__init__(message)
        self.phase = phase


class ConversionSubprocessError(RuntimeError):
    """Raised when the conversion subprocess exits unexpectedly."""

    error_code = "conversion_subprocess_failed"
    retryable: ClassVar[bool] = True


class JobDataIntegrityError(RuntimeError):
    """Raised when job data invariants are violated."""

    error_code = "job_data_integrity"
    retryable: ClassVar[bool] = False


class ReportedChildError(RuntimeError):
    """Raised when a child process reports a failure."""

    retryable: ClassVar[bool] = True

    def __init__(self, message: str, error_code: str, *, retryable: bool | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        if retryable is not None:
            self.retryable = retryable


class PreflightError(RuntimeError):
    """Raised when preflight validation fails unexpectedly."""

    error_code = "conversion_preflight_failed"
    retryable: ClassVar[bool] = True
```

- [ ] **Step 2: Create `types.py`**

```python
"""Shared data types for the conversion worker."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class ConversionInput:
    """Source bytes and processing pipeline information."""

    pipeline: Literal["html", "pdf"]
    content_bytes: bytes
    fetched_at: dt.datetime


@dataclass(frozen=True)
class ConversionArtifacts:
    """Local conversion artifacts generated in phase one."""

    markdown_path: Path
    figure_paths: list[Path]
    markdown_hash: str
    pipeline_name: str
    fetched_at: dt.datetime
    docling_version: str


@dataclass(frozen=True, slots=True)
class SupervisionResult:
    """Return values for conversion subprocess supervision."""

    last_phase: str
    reported_error: dict[str, str] | None
    cancelled: bool
    timed_out: bool


def _utcnow() -> dt.datetime:
    """Return timezone-aware UTC timestamp."""
    return dt.datetime.now(dt.timezone.utc)
```

`_utcnow` lives here because it is used by `orchestrator.py`, `uploader.py`, and `loop.py`.
Placing it in a leaf module avoids circular imports.

- [ ] **Step 3: Verify both modules import cleanly**

Run: `uv run python -c "from aizk.conversion.workers.errors import *; from aizk.conversion.workers.types import *; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add src/aizk/conversion/workers/errors.py src/aizk/conversion/workers/types.py
git commit -m "refactor(conversion/worker): extract errors.py and types.py leaf modules"
```

---

### Task 2: Create `supervision.py`

Parent-side subprocess monitoring.
No DB or config dependencies — cancellation is checked via a callback.

**Files:**

- Create: `src/aizk/conversion/workers/supervision.py`

**Boundary refinement:** The current `_supervise_conversion_process` takes `engine: Engine` and `config: ConversionConfig` to call `_is_job_cancelled(job_id, engine)` and `handle_job_error(...)`.
After extraction:

- Cancellation becomes `is_cancelled_fn: Callable[[], bool]` — the caller passes `lambda: _is_job_cancelled(job_id, engine)`.
- Timeout error handling is removed — the caller checks `result.timed_out` and handles the error (already done for cancellation).
- `engine` and `config` parameters are dropped from the signature.

This eliminates supervision's DB and config dependencies entirely.

- [ ] **Step 1: Create `supervision.py`**

```python
"""Subprocess supervision for conversion jobs."""

from __future__ import annotations

import logging
import multiprocessing as mp
import os
import queue as queue_module
import signal
import time
from collections.abc import Callable

from aizk.conversion.workers.types import SupervisionResult

logger = logging.getLogger(__name__)


def _get_parent_pgid() -> int | None:
    """Return the parent process group id, if available."""
    try:
        return os.getpgrp()
    except OSError:
        return None


def _terminate_child_process(process: mp.Process, parent_pgid: int | None, sig: int) -> None:
    """Terminate the child process or its process group safely."""
    if not process.pid:
        return
    try:
        pgid = os.getpgid(process.pid)
        if parent_pgid is not None and pgid == parent_pgid:
            os.kill(process.pid, sig)
        else:
            os.killpg(pgid, sig)
    except (ProcessLookupError, OSError):
        return


def _collect_status_messages(
    *,
    job_id: int,
    status_queue: mp.Queue,
    last_phase: str,
    reported_error: dict[str, str] | None,
) -> tuple[str, dict[str, str] | None]:
    """Drain the status queue, updating phase and error state."""
    try:
        while True:
            message = status_queue.get_nowait()
            event = message.get("event")
            if event == "phase":
                new_phase = message.get("message", last_phase)
                if new_phase != last_phase:
                    last_phase = new_phase
                    logger.info("Job %s entered phase %s", job_id, last_phase)
            elif event == "failed":
                reported_error = message
    except queue_module.Empty:
        pass
    return last_phase, reported_error


def _supervise_conversion_process(
    *,
    job_id: int,
    process: mp.Process,
    status_queue: mp.Queue,
    poll_interval_seconds: float,
    deadline: float | None,
    timeout_seconds: float,
    is_cancelled_fn: Callable[[], bool],
) -> SupervisionResult:
    """Monitor the subprocess for cancellation or timeout.

    Returns a ``SupervisionResult`` describing how the subprocess ended.
    The caller is responsible for acting on ``timed_out`` or ``cancelled``.
    """
    last_phase = "starting"
    reported_error: dict[str, str] | None = None
    parent_pgid = _get_parent_pgid()

    while process.is_alive():
        last_phase, reported_error = _collect_status_messages(
            job_id=job_id,
            status_queue=status_queue,
            last_phase=last_phase,
            reported_error=reported_error,
        )

        if is_cancelled_fn():
            _terminate_child_process(process, parent_pgid, signal.SIGTERM)
            process.join(timeout=5.0)
            if process.is_alive() and process.pid:
                _terminate_child_process(process, parent_pgid, signal.SIGKILL)
                process.join(timeout=5.0)
            logger.info("Job %s cancelled during %s", job_id, last_phase)
            return SupervisionResult(last_phase, reported_error, True, False)

        if deadline and time.monotonic() >= deadline:
            _terminate_child_process(process, parent_pgid, signal.SIGTERM)
            process.join(timeout=5.0)
            if process.is_alive() and process.pid:
                _terminate_child_process(process, parent_pgid, signal.SIGKILL)
                process.join(timeout=5.0)
            elapsed = None
            if deadline is not None:
                elapsed = time.monotonic() - (deadline - timeout_seconds)
            logger.info(
                "Job %s timed out during %s after %s seconds",
                job_id,
                last_phase,
                round(elapsed, 3) if elapsed is not None else "unknown",
            )
            return SupervisionResult(last_phase, reported_error, False, True)

        process.join(timeout=poll_interval_seconds)

    last_phase, reported_error = _collect_status_messages(
        job_id=job_id,
        status_queue=status_queue,
        last_phase=last_phase,
        reported_error=reported_error,
    )
    return SupervisionResult(last_phase, reported_error, False, False)
```

- [ ] **Step 2: Verify import**

Run: `uv run python -c "from aizk.conversion.workers.supervision import _supervise_conversion_process; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add src/aizk/conversion/workers/supervision.py
git commit -m "refactor(conversion/worker): extract supervision.py"
```

---

### Task 3: Create `uploader.py`

S3 artifact upload, hash dedup, and output record creation.

**Files:**

- Create: `src/aizk/conversion/workers/uploader.py`

- [ ] **Step 1: Create `uploader.py`**

Copy `_upload_converted` from `worker.py` lines 398–519 verbatim, with its required imports.
Replace the inline `_utcnow` call with import from `types`.

```python
"""S3 artifact upload and output record creation."""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

from sqlmodel import Session, select

from aizk.conversion.datamodel.bookmark import Bookmark as BookmarkRecord
from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.datamodel.output import ConversionOutput
from aizk.conversion.db import get_engine
from aizk.conversion.storage.manifest import (
    ManifestConfigSnapshot,
    generate_manifest,
    save_manifest,
)
from aizk.conversion.storage.s3_client import S3Client, S3Error
from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.utilities.paths import (
    figure_paths,
    markdown_path,
    metadata_path,
)
from aizk.conversion.workers.errors import ConversionArtifactsMissingError
from aizk.conversion.workers.types import _utcnow

logger = logging.getLogger(__name__)


def _upload_converted(job_id: int, workspace: Path, config: ConversionConfig) -> None:
    """Upload artifacts to S3 and record conversion output in the DB."""
    engine = get_engine(config.database_url)
    metadata_file = metadata_path(workspace)
    if not metadata_file.exists():
        raise ConversionArtifactsMissingError(f"Missing metadata for job {job_id}")

    metadata = json.loads(metadata_file.read_text())
    markdown_filename = metadata["markdown_filename"]
    markdown_file = markdown_path(workspace, markdown_filename)
    figure_files = metadata.get("figure_files", [])
    figure_file_paths = figure_paths(workspace, figure_files)

    if not markdown_file.exists():
        raise ConversionArtifactsMissingError(f"Missing markdown for job {job_id}")

    with Session(engine) as session:
        job = session.get(ConversionJob, job_id)
        if not job:
            return
        if job.status == ConversionJobStatus.CANCELLED:
            return
        bookmark = session.exec(select(BookmarkRecord).where(BookmarkRecord.aizk_uuid == job.aizk_uuid)).one()

        # Reuse existing S3 artifacts when the content hash matches a prior output for
        # the same bookmark, avoiding redundant uploads of identical content.
        new_hash = metadata["markdown_hash_xx64"]
        prior_output = session.exec(
            select(ConversionOutput)
            .where(ConversionOutput.aizk_uuid == bookmark.aizk_uuid)
            .where(ConversionOutput.markdown_hash_xx64 == new_hash)
            .order_by(ConversionOutput.created_at.desc())
        ).first()

        if prior_output is not None:
            logger.info(
                "Job %s: content hash matches prior output %s; reusing S3 artifacts at %s",
                job_id,
                prior_output.id,
                prior_output.s3_prefix,
            )
            output = ConversionOutput(
                job_id=job.id,
                aizk_uuid=bookmark.aizk_uuid,
                title=bookmark.title,
                payload_version=job.payload_version,
                s3_prefix=prior_output.s3_prefix,
                markdown_key=prior_output.markdown_key,
                manifest_key=prior_output.manifest_key,
                markdown_hash_xx64=new_hash,
                figure_count=prior_output.figure_count,
                docling_version=metadata["docling_version"],
                pipeline_name=metadata["pipeline_name"],
            )
            session.add(output)
            job.finished_at = _utcnow()
            job.status = ConversionJobStatus.SUCCEEDED
            job.error_code = None
            job.error_message = None
            job.updated_at = _utcnow()
            session.add(job)
            session.commit()
            return

        s3_client = S3Client(config)
        if not s3_client.bucket:
            raise S3Error("S3 bucket is not configured", "s3_upload_failed")

        prefix = str(bookmark.aizk_uuid)
        s3_prefix_uri = f"s3://{s3_client.bucket}/{prefix}/"
        markdown_key = f"{prefix}/{markdown_filename}"
        markdown_uri = s3_client.upload_file(markdown_file, markdown_key)

        figure_uris: list[str] = []
        for fig_path in figure_file_paths:
            if not fig_path.exists():
                continue
            fig_key = f"{prefix}/figures/{fig_path.name}"
            figure_uris.append(s3_client.upload_file(fig_path, fig_key))

        job.finished_at = _utcnow()
        manifest = generate_manifest(
            bookmark=bookmark,
            job=job,
            fetched_at=dt.datetime.fromisoformat(metadata["fetched_at"]),
            markdown_s3_uri=markdown_uri,
            markdown_hash=metadata["markdown_hash_xx64"],
            figure_s3_uris=figure_uris,
            docling_version=metadata["docling_version"],
            pipeline_name=metadata["pipeline_name"],
            config_snapshot=ManifestConfigSnapshot(**metadata["config_snapshot"]),
        )
        manifest_path = workspace / "manifest.json"
        save_manifest(manifest, manifest_path)
        manifest_uri = s3_client.upload_file(manifest_path, f"{prefix}/manifest.json")

        session.refresh(job)
        if job.status == ConversionJobStatus.CANCELLED:
            return

        output = ConversionOutput(
            job_id=job.id,
            aizk_uuid=bookmark.aizk_uuid,
            title=bookmark.title,
            payload_version=job.payload_version,
            s3_prefix=s3_prefix_uri,
            markdown_key=markdown_uri,
            manifest_key=manifest_uri,
            markdown_hash_xx64=metadata["markdown_hash_xx64"],
            figure_count=len(figure_uris),
            docling_version=metadata["docling_version"],
            pipeline_name=metadata["pipeline_name"],
        )
        session.add(output)

        job.status = ConversionJobStatus.SUCCEEDED
        job.error_code = None
        job.error_message = None
        job.updated_at = _utcnow()
        session.add(job)
        session.commit()
```

- [ ] **Step 2: Verify import**

Run: `uv run python -c "from aizk.conversion.workers.uploader import _upload_converted; print('OK')"` Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add src/aizk/conversion/workers/uploader.py
git commit -m "refactor(conversion/worker): extract uploader.py"
```

---

### Task 4: Create `orchestrator.py`

The per-job orchestration shell: preflight, subprocess spawning, child-process functions, supervision dispatch, upload dispatch, and error handling.

**Files:**

- Create: `src/aizk/conversion/workers/orchestrator.py`

**Key changes from original `worker.py`:**

1. `_supervise_conversion_process` call drops `engine` and `config`, gains `is_cancelled_fn=lambda: _is_job_cancelled(job_id, engine)`.
2. `process_job_supervised` gains explicit timeout handling after `result.timed_out` (was previously inline in supervision).

- [ ] **Step 1: Create `orchestrator.py`**

Copy all remaining functions from `worker.py` lines 153–893 (except those already extracted to supervision/uploader), with adjusted imports.

```python
"""Per-job orchestration for conversion workers."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import multiprocessing as mp
import os
from pathlib import Path
import tempfile
import time
from typing import Literal

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from aizk.conversion.datamodel.bookmark import Bookmark as BookmarkRecord
from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.db import get_engine
from aizk.conversion.utilities.bookmark_utils import (
    BookmarkContentError,
    detect_content_type,
    detect_source_type,
    fetch_karakeep_bookmark,
    get_bookmark_asset_id,
    get_bookmark_html_content,
    get_bookmark_source_url,
    get_bookmark_text_content,
    is_pdf_asset,
    is_precrawled_archive_asset,
    validate_bookmark_content,
)
from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.utilities.hashing import build_output_config_snapshot, compute_markdown_hash
from aizk.conversion.utilities.paths import (
    OUTPUT_MARKDOWN_FILENAME,
    markdown_path,
    metadata_path,
)
from aizk.conversion.utilities.whitespace import normalize_whitespace
from aizk.conversion.workers.converter import (
    ConversionError,
    convert_html,
    convert_pdf,
)
from aizk.conversion.workers.errors import (
    ConversionCancelledError,
    ConversionSubprocessError,
    ConversionTimeoutError,
    JobDataIntegrityError,
    PreflightError,
    ReportedChildError,
)
from aizk.conversion.workers.fetcher import (
    BookmarkContentUnavailableError,
    FetchError,
    fetch_arxiv,
    fetch_github_readme,
    fetch_karakeep_asset,
)
from aizk.conversion.workers.supervision import _supervise_conversion_process
from aizk.conversion.workers.types import (
    ConversionArtifacts,
    ConversionInput,
    _utcnow,
)
from aizk.conversion.workers.uploader import _upload_converted
from aizk.utilities.async_utils import run_async
from aizk.utilities.url_utils import normalize_url
from karakeep_client.models import Bookmark as KarakeepBookmark

logger = logging.getLogger(__name__)


def _docling_version() -> str:
    """Return the installed docling version for conversion metadata."""
    from importlib.metadata import version

    return version("docling")


def _raise_if_cancelled(job_id: int, engine: Engine) -> None:
    """Raise if the job status has been marked as cancelled."""
    with Session(engine) as session:
        job = session.get(ConversionJob, job_id)
        if job and job.status == ConversionJobStatus.CANCELLED:
            raise ConversionCancelledError(f"Job {job_id} cancelled")


def _is_job_cancelled(job_id: int, engine: Engine) -> bool:
    """Return True when the job status is CANCELLED."""
    with Session(engine) as session:
        job = session.get(ConversionJob, job_id)
        return bool(job and job.status == ConversionJobStatus.CANCELLED)


def _report_status(
    status_queue: mp.Queue | None,
    *,
    event: Literal["phase", "completed", "cancelled", "failed"],
    message: str,
    error_code: str | None = None,
    retryable: bool | None = None,
) -> None:
    """Send a structured event from the subprocess to the parent."""
    if not status_queue:
        return
    payload: dict[str, str] = {"event": event, "message": message}
    if error_code:
        payload["error_code"] = error_code
    if retryable is not None:
        payload["retryable"] = "true" if retryable else "false"
    try:
        status_queue.put_nowait(payload)
    except Exception:
        logger.debug(
            "Failed to report status event %s with message %s",
            event,
            message,
            exc_info=True,
        )
        return


def _prepare_bookmark_for_job(job_id: int, engine: Engine) -> tuple[BookmarkRecord, KarakeepBookmark]:
    """Fetch, validate, and persist AIZK Bookmark for conversion.

    Runs in the parent process before spawning the conversion subprocess.
    """
    with Session(engine) as session:
        job = session.get(ConversionJob, job_id)
        if not job:
            raise JobDataIntegrityError(f"Job {job_id} missing during preflight")
        bookmark = session.exec(select(BookmarkRecord).where(BookmarkRecord.aizk_uuid == job.aizk_uuid)).one()

    karakeep_bookmark = fetch_karakeep_bookmark(bookmark.karakeep_id)
    if not karakeep_bookmark:
        raise FetchError(f"Bookmark {bookmark.karakeep_id} not found in KaraKeep")
    validate_bookmark_content(karakeep_bookmark)

    source_url = get_bookmark_source_url(karakeep_bookmark)
    updated_source_type = detect_source_type(source_url)
    updated_content_type = detect_content_type(karakeep_bookmark)
    updated_title = karakeep_bookmark.title or source_url
    normalized_url = normalize_url(source_url) if source_url else None

    with Session(engine) as session:
        bookmark = session.exec(select(BookmarkRecord).where(BookmarkRecord.aizk_uuid == job.aizk_uuid)).one()
        job_record = session.get(ConversionJob, job_id)
        bookmark.url = source_url
        bookmark.normalized_url = normalized_url
        bookmark.title = updated_title
        bookmark.content_type = updated_content_type
        bookmark.source_type = updated_source_type
        bookmark.updated_at = _utcnow()
        if job_record:
            job_record.title = updated_title
            job_record.updated_at = _utcnow()
            session.add(job_record)
        session.add(bookmark)
        session.commit()
        session.refresh(bookmark)

    return bookmark, karakeep_bookmark


def _prepare_conversion_input(
    *,
    bookmark_record: BookmarkRecord,
    karakeep_bookmark: KarakeepBookmark,
    config: ConversionConfig,
) -> ConversionInput:
    """Prepare conversion input bytes and determine pipeline."""
    fetched_at = _utcnow()
    if bookmark_record.source_type == "arxiv":
        pdf_bytes = asyncio.run(fetch_arxiv(karakeep_bookmark, config))
        return ConversionInput(pipeline="pdf", content_bytes=pdf_bytes, fetched_at=fetched_at)

    if bookmark_record.source_type == "github":
        readme_bytes = asyncio.run(fetch_github_readme(karakeep_bookmark, config))
        return ConversionInput(pipeline="html", content_bytes=readme_bytes, fetched_at=fetched_at)

    if is_pdf_asset(karakeep_bookmark):
        asset_id = get_bookmark_asset_id(karakeep_bookmark)
        if asset_id:
            pdf_bytes = run_async(fetch_karakeep_asset, asset_id)
            return ConversionInput(pipeline="pdf", content_bytes=pdf_bytes, fetched_at=fetched_at)

    if is_precrawled_archive_asset(karakeep_bookmark):
        asset_id = get_bookmark_asset_id(karakeep_bookmark)
        if asset_id:
            html_bytes = run_async(fetch_karakeep_asset, asset_id)
            return ConversionInput(pipeline="html", content_bytes=html_bytes, fetched_at=fetched_at)

    # Fallback to HTML content
    html_content = get_bookmark_html_content(karakeep_bookmark)
    if html_content:
        return ConversionInput(pipeline="html", content_bytes=html_content.encode("utf-8"), fetched_at=fetched_at)

    text_content = get_bookmark_text_content(karakeep_bookmark)
    if text_content:
        html = f"<html><body><pre>{text_content}</pre></body></html>"
        return ConversionInput(pipeline="html", content_bytes=html.encode("utf-8"), fetched_at=fetched_at)

    raise BookmarkContentUnavailableError(f"Bookmark {bookmark_record.karakeep_id} has no usable content")


def _run_conversion(
    *,
    job: ConversionJob,
    bookmark: BookmarkRecord,
    conversion_input: ConversionInput,
    config: ConversionConfig,
    engine: Engine,
    workspace: Path,
) -> ConversionArtifacts:
    """Run conversion and persist local artifacts for parent upload.

    This is executed in the child process and performs cancellation checks.
    """
    _raise_if_cancelled(job.id, engine)
    content_bytes = conversion_input.content_bytes
    pipeline_name = conversion_input.pipeline
    fetched_at = conversion_input.fetched_at

    if pipeline_name == "pdf":
        markdown_text, figure_paths = convert_pdf(content_bytes, workspace, config)
    else:
        markdown_text, figure_paths = convert_html(content_bytes, workspace, config, source_url=bookmark.url)

    _raise_if_cancelled(job.id, engine)
    markdown_filename = OUTPUT_MARKDOWN_FILENAME
    markdown_file = markdown_path(workspace, markdown_filename)
    # Normalize whitespace before writing and hashing
    markdown_text = normalize_whitespace(markdown_text)
    markdown_file.write_text(markdown_text)
    markdown_hash = compute_markdown_hash(markdown_text)
    picture_description_enabled = config.is_picture_description_enabled()
    config_snapshot = build_output_config_snapshot(
        config,
        picture_description_enabled=picture_description_enabled,
    )

    metadata = {
        "pipeline_name": pipeline_name,
        "fetched_at": fetched_at.isoformat(),
        "markdown_filename": markdown_filename,
        "figure_files": [path.name for path in figure_paths],
        "markdown_hash_xx64": markdown_hash,
        "docling_version": _docling_version(),
        "config_snapshot": config_snapshot,
    }
    metadata_file = metadata_path(workspace)
    metadata_file.write_text(json.dumps(metadata, indent=2, sort_keys=False))

    return ConversionArtifacts(
        markdown_path=markdown_file,
        figure_paths=figure_paths,
        markdown_hash=markdown_hash,
        pipeline_name=pipeline_name,
        fetched_at=fetched_at,
        docling_version=metadata["docling_version"],
    )


def _convert_job_artifacts(
    *,
    job_id: int,
    workspace: Path,
    karakeep_payload_path: Path,
    status_queue: mp.Queue | None,
) -> None:
    """Prepare input and run conversion in the child process."""
    config = ConversionConfig()
    engine = get_engine(config.database_url)

    with Session(engine) as session:
        job = session.get(ConversionJob, job_id)
        if not job:
            raise JobDataIntegrityError(f"Job {job_id} missing during conversion")
        bookmark = session.exec(select(BookmarkRecord).where(BookmarkRecord.aizk_uuid == job.aizk_uuid)).one()

    _raise_if_cancelled(job_id, engine)
    karakeep_payload = json.loads(karakeep_payload_path.read_text())
    karakeep_bookmark = KarakeepBookmark.model_validate(karakeep_payload)

    _report_status(status_queue, event="phase", message="preparing_input")
    conversion_input = _prepare_conversion_input(
        bookmark_record=bookmark,
        karakeep_bookmark=karakeep_bookmark,
        config=config,
    )

    _report_status(status_queue, event="phase", message="converting")
    _run_conversion(
        job=job,
        bookmark=bookmark,
        config=config,
        workspace=workspace,
        conversion_input=conversion_input,
        engine=engine,
    )


def _process_job_subprocess(
    job_id: int,
    workspace_path: str,
    karakeep_payload_path: str,
    status_queue: mp.Queue,
) -> None:
    """Subprocess entrypoint that reports conversion events to the parent."""
    os.setpgrp()  # Create new process group for cleanup of all descendants
    try:
        _convert_job_artifacts(
            job_id=job_id,
            workspace=Path(workspace_path),
            karakeep_payload_path=Path(karakeep_payload_path),
            status_queue=status_queue,
        )
        _report_status(status_queue, event="completed", message="conversion completed")
    except ConversionCancelledError:
        _report_status(status_queue, event="cancelled", message="conversion cancelled")
    except (
        ConversionError,
        FetchError,
        BookmarkContentUnavailableError,
        BookmarkContentError,
        JobDataIntegrityError,
    ) as exc:
        error_code = getattr(exc, "error_code", "conversion_failed")
        retryable = exc.retryable
        _report_status(
            status_queue,
            event="failed",
            message=str(exc),
            error_code=error_code,
            retryable=retryable,
        )
        raise
    except Exception as exc:
        _report_status(
            status_queue,
            event="failed",
            message=str(exc),
            error_code="conversion_failed",
            retryable=True,
        )
        raise


def _spawn_conversion_subprocess(
    *,
    job_id: int,
    workspace: Path,
    payload_path: Path,
) -> tuple[mp.Process, mp.Queue]:
    """Start the conversion subprocess and return the process and status queue."""
    ctx = mp.get_context("spawn")
    status_queue: mp.Queue = ctx.Queue()
    process = ctx.Process(
        target=_process_job_subprocess,
        args=(job_id, str(workspace), str(payload_path), status_queue),
        daemon=True,
    )
    process.start()
    return process, status_queue


def _initialize_running_job(job_id: int, engine: Engine) -> bool:
    """Ensure the job is in RUNNING state before processing."""
    with Session(engine) as session:
        job = session.get(ConversionJob, job_id)
        if not job:
            return False
        if job.status in {ConversionJobStatus.SUCCEEDED, ConversionJobStatus.CANCELLED}:
            return False
        if job.status != ConversionJobStatus.RUNNING:
            job.status = ConversionJobStatus.RUNNING
            job.started_at = _utcnow()
            job.attempts += 1
            job.updated_at = _utcnow()
            session.add(job)
            session.commit()
    return True


def process_job_supervised(job_id: int, config: ConversionConfig, poll_interval_seconds: float = 2.0) -> None:
    """Run a supervised conversion attempt and upload artifacts on success.

    The parent process handles preflight, cancellation, timeout, and uploads.
    """
    engine = get_engine(config.database_url)
    timeout_seconds = float(config.worker_job_timeout_seconds)

    if not _initialize_running_job(job_id, engine):
        return

    try:
        bookmark, karakeep_bookmark = _prepare_bookmark_for_job(job_id, engine)
    except (FetchError, BookmarkContentError, JobDataIntegrityError) as exc:
        handle_job_error(job_id, exc, config)
        return
    except Exception as exc:
        handle_job_error(job_id, PreflightError(f"Job {job_id} preflight failed: {exc}"), config)
        return
    with tempfile.TemporaryDirectory() as tmpdirname:
        workspace = Path(tmpdirname)
        payload_path = workspace / "karakeep_bookmark.json"
        payload_path.write_text(json.dumps(karakeep_bookmark.model_dump(mode="json"), indent=2))

        process, status_queue = _spawn_conversion_subprocess(
            job_id=job_id,
            workspace=workspace,
            payload_path=payload_path,
        )

        deadline = None
        if timeout_seconds > 0:
            deadline = time.monotonic() + timeout_seconds

        result = _supervise_conversion_process(
            job_id=job_id,
            process=process,
            status_queue=status_queue,
            poll_interval_seconds=poll_interval_seconds,
            deadline=deadline,
            timeout_seconds=timeout_seconds,
            is_cancelled_fn=lambda: _is_job_cancelled(job_id, engine),
        )

        if result.timed_out:
            handle_job_error(
                job_id,
                ConversionTimeoutError(
                    f"Job {job_id} exceeded its runtime during {result.last_phase}",
                    result.last_phase,
                ),
                config,
            )
            return

        if result.cancelled:
            return

        if result.reported_error:
            error_code = result.reported_error.get("error_code", "conversion_failed")
            error_message = result.reported_error.get("message", "conversion_failed")
            retryable = None
            retryable_value = result.reported_error.get("retryable")
            if retryable_value is not None:
                retryable = str(retryable_value).lower() == "true"
            handle_job_error(
                job_id,
                ReportedChildError(
                    error_message,
                    error_code,
                    retryable=retryable,
                ),
                config,
            )
            return

        if process.exitcode and process.exitcode != 0:
            handle_job_error(
                job_id,
                ConversionSubprocessError(f"Job {job_id} subprocess exited with code {process.exitcode}"),
                config,
            )
            return

        if _is_job_cancelled(job_id, engine):
            logger.info("Job %s cancelled before upload", job_id)
            return

        last_phase = "uploading"
        if deadline and time.monotonic() >= deadline:
            elapsed = time.monotonic() - (deadline - timeout_seconds)
            logger.info(
                "Job %s timed out during %s after %s seconds",
                job_id,
                last_phase,
                round(elapsed, 3),
            )
            handle_job_error(
                job_id,
                ConversionTimeoutError(
                    f"Job {job_id} exceeded its runtime during {last_phase}",
                    last_phase,
                ),
                config,
            )
            return

        with Session(engine) as session:
            job_record = session.get(ConversionJob, job_id)
            if job_record:
                job_record.status = ConversionJobStatus.UPLOAD_PENDING
                job_record.updated_at = _utcnow()
                session.add(job_record)
                session.commit()

        for attempt in range(1, config.retry_max_attempts + 1):
            if deadline and time.monotonic() >= deadline:
                elapsed = time.monotonic() - (deadline - timeout_seconds)
                logger.info(
                    "Job %s timed out during %s after %s seconds",
                    job_id,
                    last_phase,
                    round(elapsed, 3),
                )
                handle_job_error(
                    job_id,
                    ConversionTimeoutError(
                        f"Job {job_id} exceeded its runtime during {last_phase}",
                        last_phase,
                    ),
                    config,
                )
                return
            try:
                _upload_converted(job_id, workspace, config)
                break
            except Exception as exc:
                if attempt == config.retry_max_attempts:
                    handle_job_error(job_id, exc, config)
                    break
                delay = config.retry_base_delay_seconds * (2 ** (attempt - 1))
                logger.warning(
                    "Upload attempt %d failed for job %s; retrying in %s seconds: %s",
                    attempt,
                    job_id,
                    delay,
                    exc,
                )
                time.sleep(delay)


def handle_job_error(job_id: int, error: Exception, config: ConversionConfig) -> None:
    """Persist job failure details and compute retryability.

    Retry decision uses the `retryable` class attribute on every exception class.
    """
    engine = get_engine(config.database_url)
    now = _utcnow()

    error_code = getattr(error, "error_code", "conversion_failed")
    message = str(error)

    retryable: bool = error.retryable  # type: ignore[attr-defined]

    with Session(engine) as session:
        job = session.get(ConversionJob, job_id)
        if not job:
            return
        if job.status == ConversionJobStatus.CANCELLED:
            return
        if retryable:
            delay = config.retry_base_delay_seconds * (2**job.attempts)
            job.status = ConversionJobStatus.FAILED_RETRYABLE
            job.earliest_next_attempt_at = now + dt.timedelta(seconds=delay)
        else:
            job.status = ConversionJobStatus.FAILED_PERM
            job.earliest_next_attempt_at = None
            job.finished_at = now
        job.error_code = error_code
        job.error_message = message
        job.last_error_at = now
        job.updated_at = now
        session.add(job)
        session.commit()
```

- [ ] **Step 2: Verify import**

Run: `uv run python -c "from aizk.conversion.workers.orchestrator import process_job_supervised; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add src/aizk/conversion/workers/orchestrator.py
git commit -m "refactor(conversion/worker): extract orchestrator.py"
```

---

### Task 5: Create `loop.py`

The outer polling loop, stale job recovery, and `run_worker` entry point.

**Files:**

- Create: `src/aizk/conversion/workers/loop.py`

- [ ] **Step 1: Create `loop.py`**

```python
"""Worker polling loop and stale job recovery."""

from __future__ import annotations

import datetime as dt
import logging
import time

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlmodel import Session, select

from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.db import get_engine
from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.workers.orchestrator import process_job_supervised
from aizk.conversion.workers.types import _utcnow

logger = logging.getLogger(__name__)


def recover_stale_running_jobs(config: ConversionConfig) -> int:
    """Mark stale RUNNING jobs as retryable.

    This can catch jobs that were being processed when a worker crashed.
    """
    engine = get_engine(config.database_url)
    now = _utcnow()
    stale_before = now - dt.timedelta(minutes=config.worker_stale_job_minutes)

    with Session(engine) as session:
        jobs = session.exec(
            select(ConversionJob)
            .where(ConversionJob.status == ConversionJobStatus.RUNNING)
            .where(ConversionJob.started_at.is_not(None))  # type: ignore[operator]
            .where(ConversionJob.started_at < stale_before)
        ).all()

        if not jobs:
            return 0

        for job in jobs:
            job.status = ConversionJobStatus.FAILED_RETRYABLE
            job.earliest_next_attempt_at = now
            job.error_code = "worker_stale_running"
            job.error_message = f"Marked stale after {config.worker_stale_job_minutes} minutes without completion."
            job.last_error_at = now
            job.updated_at = now
            session.add(job)

        session.commit()

    return len(jobs)


def poll_and_process_jobs(config: ConversionConfig, poll_interval_seconds: float = 2.0) -> bool:
    """Pick up the next eligible job and invoke supervised processing."""
    engine = get_engine(config.database_url)
    now = _utcnow()

    with Session(engine) as session:
        try:
            # BEGIN IMMEDIATE prevents multiple workers from selecting the same job.
            session.exec(text("BEGIN IMMEDIATE"))
            job = session.exec(
                select(ConversionJob)
                .where(ConversionJob.status.in_([ConversionJobStatus.QUEUED, ConversionJobStatus.FAILED_RETRYABLE]))
                .where(
                    (ConversionJob.earliest_next_attempt_at.is_(None))  # type: ignore[operator]
                    | (ConversionJob.earliest_next_attempt_at <= now)
                )
                .order_by(ConversionJob.queued_at)
            ).first()
        except OperationalError as exc:
            session.rollback()
            logger.warning("Job poll skipped due to database lock: %s", exc)
            return False
        except DBAPIError:
            session.rollback()
            logger.exception("Job poll failed due to database error")
            return False

        if not job:
            session.rollback()
            return False

        job_id = job.id
        job.status = ConversionJobStatus.RUNNING
        job.started_at = now
        job.attempts += 1
        job.updated_at = now
        session.add(job)
        session.commit()

    if job_id is None:
        raise RuntimeError("Queued job missing id; cannot process job")

    process_job_supervised(job_id, config, poll_interval_seconds=poll_interval_seconds)
    return True


def run_worker(config: ConversionConfig, poll_interval_seconds: float = 2.0) -> None:
    """Run the worker loop for polling, processing, and recovery."""
    logger.info("Starting conversion worker loop")
    last_recovery_check = 0.0
    while True:
        now = time.monotonic()
        if now - last_recovery_check >= config.worker_stale_job_check_seconds:
            recovered = recover_stale_running_jobs(config)
            if recovered:
                logger.warning("Recovered %d stale RUNNING jobs", recovered)
            last_recovery_check = now
        processed = poll_and_process_jobs(config, poll_interval_seconds=poll_interval_seconds)
        if not processed:
            time.sleep(poll_interval_seconds)
```

- [ ] **Step 2: Verify import**

Run: `uv run python -c "from aizk.conversion.workers.loop import run_worker; print('OK')"` Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add src/aizk/conversion/workers/loop.py
git commit -m "refactor(conversion/worker): extract loop.py"
```

---

### Task 6: Replace `worker.py` with re-export shim, update all imports, run tests

Replace `worker.py` with a re-export shim, update `cli.py` and all test files to import from the correct new modules, then run the full test suite.

**Files:**

- Modify: `src/aizk/conversion/workers/worker.py` (replace with shim)

- Modify: `src/aizk/conversion/cli.py:67`

- Modify: `tests/conversion/unit/test_worker.py`

- Modify: `tests/conversion/integration/test_worker_concurrency.py`

- Modify: `tests/conversion/integration/test_whitespace_normalization.py`

- Modify: `tests/conversion/integration/test_worker_lifecycle.py`

- Modify: `tests/conversion/integration/test_conversion_flow.py`

- [ ] **Step 1: Replace `worker.py` with re-export shim**

```python
"""Re-export shim for backwards compatibility during worker decomposition.

All new code should import from the specific submodules directly:
- errors: Exception classes
- types: Data types (ConversionInput, ConversionArtifacts, SupervisionResult)
- supervision: Subprocess monitoring
- uploader: S3 upload and output records
- orchestrator: Per-job orchestration
- loop: Worker polling loop
"""

from aizk.conversion.workers.errors import *  # noqa: F401, F403
from aizk.conversion.workers.loop import *  # noqa: F401, F403
from aizk.conversion.workers.orchestrator import *  # noqa: F401, F403
from aizk.conversion.workers.types import *  # noqa: F401, F403
```

This shim re-exports public names (no leading underscore) for any code that still does `from aizk.conversion.workers.worker import ConversionInput`.
It does NOT support `monkeypatch.setattr(worker, "_upload_converted", ...)` — those must be updated.

- [ ] **Step 2: Update `cli.py`**

Change:

```python
        from aizk.conversion.workers.worker import run_worker
```

To:

```python
        from aizk.conversion.workers.loop import run_worker
```

- [ ] **Step 3: Update test imports and patch targets**

The key principle: **patch at the import location used by the unit under test.**
After decomposition, functions that `process_job_supervised` calls are looked up in `orchestrator`'s namespace.
Functions that `_supervise_conversion_process` calls are looked up in `supervision`'s namespace.
Tests must patch the correct module.

**Patch target mapping** (applies to ALL test files):

| Current patch target                     | New patch target                                                       | Reason                                                                             |
| ---------------------------------------- | ---------------------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| `worker.fetch_karakeep_bookmark`         | `orchestrator.fetch_karakeep_bookmark`                                 | Called in orchestrator's `_prepare_bookmark_for_job`                               |
| `worker.validate_bookmark_content`       | `orchestrator.validate_bookmark_content`                               | Called in orchestrator's `_prepare_bookmark_for_job`                               |
| `worker._prepare_conversion_input`       | `orchestrator._prepare_conversion_input`                               | Called in orchestrator's `_convert_job_artifacts`                                  |
| `worker._run_conversion`                 | `orchestrator._run_conversion`                                         | Called in orchestrator's `_convert_job_artifacts`                                  |
| `worker._upload_converted`               | `orchestrator._upload_converted`                                       | Imported into orchestrator from uploader                                           |
| `worker.handle_job_error`                | `orchestrator.handle_job_error`                                        | Defined in orchestrator                                                            |
| `worker.process_job_supervised`          | `orchestrator.process_job_supervised` or `loop.process_job_supervised` | `orchestrator` if testing orchestrator directly; `loop` if testing poll's dispatch |
| `worker.poll_and_process_jobs`           | `loop.poll_and_process_jobs`                                           | Defined in loop                                                                    |
| `worker.recover_stale_running_jobs`      | `loop.recover_stale_running_jobs`                                      | Defined in loop                                                                    |
| `worker._is_job_cancelled`               | `orchestrator._is_job_cancelled`                                       | Defined in orchestrator; supervision uses callback                                 |
| `worker._supervise_conversion_process`   | `orchestrator._supervise_conversion_process`                           | Imported into orchestrator from supervision                                        |
| `worker._spawn_conversion_subprocess`    | `orchestrator._spawn_conversion_subprocess`                            | Defined in orchestrator                                                            |
| `worker._process_job_subprocess`         | `orchestrator._process_job_subprocess`                                 | Defined in orchestrator                                                            |
| `worker._convert_job_artifacts`          | `orchestrator._convert_job_artifacts`                                  | Defined in orchestrator                                                            |
| `worker._report_status`                  | `orchestrator._report_status`                                          | Defined in orchestrator                                                            |
| `worker._initialize_running_job`         | `orchestrator._initialize_running_job`                                 | Defined in orchestrator                                                            |
| `worker._utcnow`                         | `loop._utcnow`                                                         | Imported into loop from types; patch loop's reference for poll tests               |
| `worker.get_engine`                      | `orchestrator.get_engine`                                              | Imported into orchestrator                                                         |
| `worker.S3Client`                        | `uploader.S3Client`                                                    | Imported into uploader                                                             |
| `worker.mp`                              | `orchestrator.mp`                                                      | Same module object; patching via any module works identically                      |
| `worker.time`                            | `orchestrator.time`                                                    | Same module object; patching via any module works identically                      |
| `worker.queue_module`                    | Replace with `import queue as queue_module` in test                    | Only used in test helper classes for `queue_module.Queue()`                        |
| `worker.tempfile`                        | `orchestrator.tempfile`                                                | Same module object                                                                 |
| `worker.ConversionTimeoutError`          | `errors.ConversionTimeoutError`                                        | For `isinstance` checks in assertions                                              |
| `worker.ConversionCancelledError`        | `errors.ConversionCancelledError`                                      | For `isinstance` and `pytest.raises`                                               |
| `worker.ConversionSubprocessError`       | `errors.ConversionSubprocessError`                                     | For `isinstance` checks                                                            |
| `worker.ReportedChildError`              | `errors.ReportedChildError`                                            | For `isinstance` checks                                                            |
| `worker.ConversionArtifactsMissingError` | `errors.ConversionArtifactsMissingError`                               | For `isinstance` checks                                                            |
| `worker.JobDataIntegrityError`           | `errors.JobDataIntegrityError`                                         | For `isinstance` checks                                                            |
| `worker.PreflightError`                  | `errors.PreflightError`                                                | For `isinstance` checks                                                            |
| `worker.SupervisionResult`               | `types.SupervisionResult`                                              | For constructing stubs                                                             |
| `worker.ConversionInput`                 | `types.ConversionInput`                                                | Direct import already works via shim                                               |

**Per-file import changes:**

**`tests/conversion/unit/test_worker.py`:**

```python
# Old:
from aizk.conversion.workers import converter, fetcher, worker
from aizk.conversion.workers.worker import ConversionInput

# New:
import queue as queue_module

from aizk.conversion.workers import converter, fetcher
from aizk.conversion.workers import errors, loop, orchestrator, types
from aizk.conversion.workers.types import ConversionInput, SupervisionResult
```

Then replace all `worker.X` references per the mapping table.
Replace `worker.queue_module.Queue()` with `queue_module.Queue()` in test helper classes.

**`tests/conversion/integration/test_worker_concurrency.py`:**

```python
# Old:
from aizk.conversion.workers import worker as worker_module

# New:
from aizk.conversion.workers import loop as loop_module
from aizk.conversion.workers import orchestrator as orchestrator_module
```

Then: `worker_module.process_job_supervised` → `orchestrator_module.process_job_supervised`, `worker_module.poll_and_process_jobs` → `loop_module.poll_and_process_jobs`.

Note: the `monkeypatch.setattr(worker_module, "process_job_supervised", ...)` patches the function that `poll_and_process_jobs` calls.
Since `poll_and_process_jobs` is in `loop.py` and imports `process_job_supervised` from `orchestrator`, the patch target is `loop_module.process_job_supervised` (patching loop's imported reference).

**`tests/conversion/integration/test_whitespace_normalization.py`:**

```python
# Old:
from aizk.conversion.workers import worker
from aizk.conversion.workers.worker import ConversionInput

# New:
import queue as queue_module

from aizk.conversion.workers import orchestrator, uploader
from aizk.conversion.workers.types import ConversionInput
```

Then replace `worker.X` per mapping.
Replace `worker.queue_module.Queue()` with `queue_module.Queue()`.

**`tests/conversion/integration/test_worker_lifecycle.py`:**

```python
# Old:
from aizk.conversion.workers import worker

# New:
from aizk.conversion.workers import errors, loop, orchestrator
```

Then replace `worker.X` per mapping. `worker.ConversionTimeoutError` → `errors.ConversionTimeoutError`. `worker.recover_stale_running_jobs` → `loop.recover_stale_running_jobs`.

**`tests/conversion/integration/test_conversion_flow.py`:**

```python
# Old:
from aizk.conversion.workers import worker
from aizk.conversion.workers.worker import ConversionInput

# New:
import queue as queue_module

from aizk.conversion.workers import orchestrator, uploader
from aizk.conversion.workers.types import ConversionInput
```

Then replace `worker.X` per mapping.
Replace `worker.queue_module.Queue()` with `queue_module.Queue()`.

- [ ] **Step 4: Run full test suite**

Run: `uv run pytest tests/conversion/ -v` Expected: All tests pass.

- [ ] **Step 5: Run lint**

Run: `uv run ruff check src/aizk/conversion/workers/ tests/conversion/`
Expected: No errors (or only pre-existing ones).

- [ ] **Step 6: Commit**

```bash
git add -A src/aizk/conversion/workers/ src/aizk/conversion/cli.py tests/conversion/
git commit -m "refactor(conversion/worker): wire decomposed modules, update all imports"
```

---

### Task 7: Archive the change

- [ ] **Step 1: Archive the change** using `sdd-archive`.
