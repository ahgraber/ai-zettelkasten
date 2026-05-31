"""Integration tests for the conversion subprocess helpers.

Covers: cancellation preflight (_raise_if_cancelled), process-group/setpgrp
setup (_process_job_subprocess), and spawn/supervise behaviour
(_spawn_and_supervise, _is_job_cancelled).
"""

from __future__ import annotations

import queue as queue_module

import pytest
from sqlmodel import Session

from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.processing import errors as errors_mod, subproc
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
        aizk_uuid=bookmark.aizk_uuid,
        owner_id="self",
        title=bookmark.title,
        idempotency_key="d" * 64,
        status=ConversionJobStatus.CANCELLED,
    )
    db_session.add(job)
    db_session.commit()

    with pytest.raises(errors_mod.ConversionCancelledError):
        subproc._raise_if_cancelled(job.id, db_session.get_bind())


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
    import os

    setpgrp_called: list[bool] = []
    monkeypatch.setattr(os, "setpgrp", lambda: setpgrp_called.append(True))

    def _short_circuit() -> None:
        raise errors_mod.ConversionCancelledError("short-circuit before any real work")

    monkeypatch.setattr(subproc, "load_process_dotenv_once", _short_circuit)

    subproc._process_job_subprocess(
        job_id=1,
        workspace_path="/unused",  # never read — the unit-of-work short-circuits first
        source_ref_json='{"kind":"url","url":"https://example.com"}',
        status_queue=queue_module.Queue(),
    )

    assert setpgrp_called == [True], "os.setpgrp() runs once at subprocess start, before any work"


def test_worker_does_not_recompute_idempotency_key():
    """workers/subproc must not call compute_idempotency_key (API owns it)."""
    import inspect

    from aizk.conversion.processing import subproc

    assert "compute_idempotency_key" not in inspect.getsource(subproc)
