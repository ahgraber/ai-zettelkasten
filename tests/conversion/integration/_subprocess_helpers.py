"""Subprocess target functions for worker lifecycle integration tests.

These functions must live in a module with only stdlib imports.  When
multiprocessing uses the ``spawn`` start method the child process reimports
the module that defines the target function.  Placing the helpers here
prevents the child from loading the full aizk / CUDA module graph on every
spawn, keeping test startup fast on GPU machines.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def test_process_subprocess(
    job_id: int,
    workspace_path: str,
    source_ref_json: str,
    status_queue,
) -> None:
    """Minimal subprocess: report converting, sleep, report completed."""
    try:
        os.setpgrp()
    except OSError:
        pass
    if status_queue:
        status_queue.put_nowait({"event": "phase", "message": "converting"})
    sleep_seconds = float(os.getenv("WORKER_TEST_SLEEP_SECONDS", "0"))
    if sleep_seconds > 0:
        time.sleep(sleep_seconds)
    if status_queue:
        status_queue.put_nowait({"event": "completed", "message": "conversion completed"})


def process_job_subprocess_success(
    job_id: int,
    workspace_path: str,
    source_ref_json: str,
    status_queue,
) -> None:
    """Subprocess that writes valid markdown + metadata, then reports completion.

    Spawn-safe target for the runner end-to-end SUCCEEDED proof: it produces the
    same artifact shape the real converter subprocess writes (an ``output.md`` and
    a ``metadata.json`` validating against ``SubprocessMetadata``) so the adapter's
    ``execute`` runs its real enrichment + upload phases to completion. Imports
    ``aizk`` hashing/path utilities at call time (the child reimports this module
    on ``spawn``); the markdown body is read from ``WORKER_TEST_SUCCESS_MARKDOWN``
    so the test can assert the uploaded content. ``WORKER_TEST_SLEEP_SECONDS``
    delays completion (after artifacts are written) so the runner can observe the
    unit in flight — used by the graceful-drain proof.
    """
    from aizk.conversion.utilities.hashing import compute_markdown_hash
    from aizk.conversion.utilities.paths import OUTPUT_MARKDOWN_FILENAME, metadata_path
    from aizk.conversion.utilities.whitespace import normalize_whitespace

    try:
        os.setpgrp()
    except OSError:
        pass

    markdown = os.environ.get("WORKER_TEST_SUCCESS_MARKDOWN", "# Runner Success\n")
    bookmark_id = os.environ.get("WORKER_TEST_SUCCESS_BOOKMARK_ID", "bm_runner_success")
    workspace = Path(workspace_path)
    normalized = normalize_whitespace(markdown)
    (workspace / OUTPUT_MARKDOWN_FILENAME).write_text(normalized)

    metadata = {
        "pipeline_name": "html",
        "fetched_at": "2024-01-01T00:00:00+00:00",
        "markdown_filename": OUTPUT_MARKDOWN_FILENAME,
        "figure_files": [],
        "markdown_hash_xx64": compute_markdown_hash(normalized),
        "docling_version": "test",
        "config_snapshot": {"converter_name": "docling"},
        "terminal_ref": {"kind": "karakeep_bookmark", "bookmark_id": bookmark_id},
        "content_type": "html",
        "source_meta": {},
        "document_title": None,
        "source_title": None,
    }
    metadata_path(workspace).write_text(json.dumps(metadata))

    if status_queue:
        status_queue.put_nowait({"event": "phase", "message": "converting"})
    sleep_seconds = float(os.getenv("WORKER_TEST_SLEEP_SECONDS", "0"))
    if sleep_seconds > 0:
        time.sleep(sleep_seconds)
    if status_queue:
        status_queue.put_nowait({"event": "completed", "message": "conversion completed"})


def process_job_subprocess_spawn_child(
    job_id: int,
    workspace_path: str,
    source_ref_json: str,
    status_queue,
) -> None:
    """Subprocess that spawns a grandchild and records both PIDs."""
    try:
        os.setpgrp()
    except OSError:
        pass
    if status_queue:
        status_queue.put_nowait({"event": "phase", "message": "converting"})
    pid_file = os.environ.get("WORKER_TEST_PID_FILE")
    if not pid_file:
        raise RuntimeError("Missing WORKER_TEST_PID_FILE")
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])  # noqa: S603
    Path(pid_file).write_text(f"{os.getpid()},{child.pid}")
    time.sleep(60)


def process_job_subprocess_graceful_sigterm(
    job_id: int,
    workspace_path: str,
    source_ref_json: str,
    status_queue,
) -> None:
    """Subprocess that writes a marker file on SIGTERM and exits cleanly."""
    try:
        os.setpgrp()
    except OSError:
        pass
    marker = os.environ.get("WORKER_TEST_MARKER_PATH")
    if not marker:
        raise RuntimeError("Missing WORKER_TEST_MARKER_PATH")
    ready_marker = os.environ.get("WORKER_TEST_READY_PATH")
    if ready_marker:
        Path(ready_marker).write_text("ready")

    def _handle(_signum, _frame):
        Path(marker).write_text("terminated")
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handle)
    if status_queue:
        status_queue.put_nowait({"event": "phase", "message": "converting"})
    while True:
        time.sleep(1)


def process_job_subprocess_ignore_sigterm(
    job_id: int,
    workspace_path: str,
    source_ref_json: str,
    status_queue,
) -> None:
    """Subprocess that ignores SIGTERM, requiring SIGKILL to terminate."""
    try:
        os.setpgrp()
    except OSError:
        pass
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if status_queue:
        status_queue.put_nowait({"event": "phase", "message": "converting"})
    while True:
        time.sleep(1)
