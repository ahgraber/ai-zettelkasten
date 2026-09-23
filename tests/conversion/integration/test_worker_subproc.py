"""Integration tests for the conversion subprocess helpers.

Covers: cancellation preflight (_raise_if_cancelled), process-group/setpgrp
setup (_process_job_subprocess), and spawn/supervise behaviour
(_spawn_and_supervise, _is_job_cancelled).
"""

from __future__ import annotations

import os
from pathlib import Path
import queue as queue_module
import time

from pyleak import no_thread_leaks
import pytest
import setproctitle
from sqlmodel import Session

from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.processing import errors as errors_mod
from aizk.conversion.processing.supervision import _terminate_and_wait
from aizk.conversion.wiring import subprocess as wiring_subprocess
from tests.conversion._helpers import make_source


def _create_bookmark(db_session: Session):
    return make_source(
        db_session,
        "bm_poll_retryable",
        url="https://example.com",
        title="Poll Retryable",
        content_type="html",
        source_type="web",
    )


# ---------------------------------------------------------------------------
# Cancellation preflight
# ---------------------------------------------------------------------------


def test_raise_if_cancelled_raises(db_session: Session) -> None:
    """Raise a cancellation exception when a job is already cancelled."""
    bookmark = _create_bookmark(db_session)
    job = ConversionJob(
        source_id=bookmark.source_id,
        owner_id="self",
        title=bookmark.title,
        idempotency_key="d" * 64,
        status=ConversionJobStatus.CANCELLED,
    )
    db_session.add(job)
    db_session.commit()

    with pytest.raises(errors_mod.ConversionCancelledError):
        wiring_subprocess._raise_if_cancelled(job.id, db_session.get_bind())


# ---------------------------------------------------------------------------
# Subprocess process-group setup
# ---------------------------------------------------------------------------


def test_process_group_creation_called_in_subprocess(monkeypatch) -> None:
    """The real subprocess entrypoint calls os.setpgrp() before any work.

    Drives the real ``_process_job_subprocess`` (not a stand-in) with its
    unit-of-work short-circuited at its first line — ``load_process_dotenv_once``
    raises ``ConversionCancelledError``, which the entrypoint reports and
    swallows — so no config, DB, or conversion runs. ``setpgrp()`` is the very
    first statement, so it must already have run when the work short-circuits,
    proving the process-group setup happens up front (its descendants can be
    cleaned up even if conversion later hangs).
    """
    setpgrp_called: list[bool] = []
    monkeypatch.setattr(wiring_subprocess.os, "setpgrp", lambda: setpgrp_called.append(True))
    monkeypatch.setattr(wiring_subprocess, "set_process_title", lambda _title: None)

    def _short_circuit() -> None:
        raise errors_mod.ConversionCancelledError("short-circuit before any real work")

    monkeypatch.setattr(wiring_subprocess, "load_process_dotenv_once", _short_circuit)

    wiring_subprocess._process_job_subprocess(
        job_id=1,
        workspace_path="/unused",  # never read — the unit-of-work short-circuits first
        source_ref_json='{"kind":"url","url":"https://example.com"}',
        status_queue=queue_module.Queue(),
    )

    assert setpgrp_called == [True], "os.setpgrp() runs once at subprocess start, before any work"


def test_child_entry_sets_a_process_title_naming_the_job(monkeypatch) -> None:
    """The child entry sets one process title that names its job and differs from the parent's title."""
    titles: list[str] = []
    monkeypatch.setattr(wiring_subprocess.os, "setpgrp", lambda: None)
    monkeypatch.setattr(wiring_subprocess, "set_process_title", titles.append)

    def _short_circuit() -> None:
        raise errors_mod.ConversionCancelledError("short-circuit before any real work")

    monkeypatch.setattr(wiring_subprocess, "load_process_dotenv_once", _short_circuit)

    wiring_subprocess._process_job_subprocess(
        job_id=4242,
        workspace_path="/unused",
        source_ref_json='{"kind":"url","url":"https://example.com"}',
        status_queue=queue_module.Queue(),
    )

    assert len(titles) == 1
    assert "4242" in titles[0]
    assert titles[0] != setproctitle.getproctitle()


@pytest.mark.isolate
@pytest.mark.integration_lifecycle
def test_cancel_within_the_first_second_reaches_the_child_process_group() -> None:
    """A cancel issued within the first second of a job terminates the child through its own process group."""
    with no_thread_leaks(action="raise", grace_period=1.0):
        process, status_queue = wiring_subprocess._spawn_conversion_subprocess(
            job_id=1,
            workspace=Path("/unused"),
            source_ref_json='{"kind":"url","url":"https://example.com"}',
        )
        try:
            deadline = time.monotonic() + 1.0
            while os.getpgid(process.pid) != process.pid and time.monotonic() < deadline:
                time.sleep(0.01)
            assert os.getpgid(process.pid) == process.pid

            _terminate_and_wait(process, parent_pgid=os.getpgid(0))
        finally:
            status_queue.close()

    assert not process.is_alive()
    assert process.exitcode is not None


def test_worker_does_not_recompute_idempotency_key():
    """workers/subproc must not call compute_idempotency_key (API owns it)."""
    import inspect

    from aizk.conversion.processing import subproc
    from aizk.conversion.wiring import subprocess as wiring_subprocess

    assert "compute_idempotency_key" not in inspect.getsource(wiring_subprocess)
    assert "compute_idempotency_key" not in inspect.getsource(subproc)
