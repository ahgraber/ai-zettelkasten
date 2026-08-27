"""Integration tests for the conversion stage as a registered console stage.

Conversion is the first console stage backed by a genuinely different backend:
owner-scoped work-units, a native status enum distinct from the generic lifecycle
vocabulary, a KaraKeep column and searchable identifier, a third (Delete) action,
and a ConversionOutput drill-down. These tests drive the real graph operator app
(where the console is served) over the shared migration-built database and pin the
conversion-specific behaviors: the Delete action and its eligibility, native-status
display and title fallback, KaraKeep search, stage-native cancel eligibility, action
equivalence with the shared domain helper, the lossless dashboard rollup, and the
owner-scoping the conversion JSON API also enforces. A final static check guards the
descriptor's import boundary.
"""

from __future__ import annotations

import ast
import datetime as dt
import pathlib
import re
from uuid import UUID

from sqlmodel import Session, select

from fastapi.testclient import TestClient

import aizk.console.stages.conversion as conversion_stage
from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.datamodel.output import ConversionOutput
from aizk.conversion.job_actions import apply_job_retry
from aizk.pipeline.events import PipelineEvent

_CONVERSION_STAGE = "conversion"


def _dashboard_row(body: str, stage_key: str) -> str:
    """Return the dashboard table row for ``stage_key`` (fails if absent)."""
    match = re.search(rf'<tr data-stage="{stage_key}".*?</tr>', body, re.DOTALL)
    assert match is not None, f"no dashboard row rendered for stage {stage_key!r}"
    return match.group(0)


# --- Delete action ------------------------------------------------------------


def test_delete_removes_terminal_jobs_and_output_and_skips_active(
    client: TestClient, db_session: Session, seed_conversion_job, seed_source, seed_conversion_output
) -> None:
    """Delete removes terminal jobs and their output, skips an active job, and reports both."""
    source = seed_source(db_session, karakeep_id="bm_del", title="Delete Doc")
    terminal = seed_conversion_job(
        db_session, source_id=source.source_id, idempotency_key="del-terminal", status=ConversionJobStatus.FAILED_PERM
    )
    seed_conversion_output(db_session, job_id=terminal.id, source_id=source.source_id)
    active = seed_conversion_job(
        db_session, source_id=source.source_id, idempotency_key="del-active", status=ConversionJobStatus.RUNNING
    )
    terminal_id = terminal.id

    response = client.post(
        "/ui/tasks/conversion/actions", data={"action": "delete", "job_ids": [terminal_id, active.id]}
    )

    assert response.status_code == 200
    assert "1 job deleted" in response.text
    assert "1 skipped as ineligible" in response.text
    db_session.expire_all()
    assert db_session.get(ConversionJob, terminal_id) is None
    assert db_session.exec(select(ConversionOutput).where(ConversionOutput.job_id == terminal_id)).first() is None
    assert db_session.get(ConversionJob, active.id).status is ConversionJobStatus.RUNNING


def test_graph_stage_rejects_delete_as_undeclared(
    client: TestClient, db_session: Session, seed_source, seed_contextualization_job
) -> None:
    """A graph stage declares no Delete; the console rejects it as undeclared (400)."""
    source = seed_source(db_session, karakeep_id="bm_no_delete", title="No-Delete Doc")
    job = seed_contextualization_job(db_session, source_id=source.source_id, conversion_output_id=1)

    response = client.post("/ui/tasks/contextualization/actions", data={"action": "delete", "job_ids": [job.id]})

    assert response.status_code == 400


# --- native-status display and title fallback --------------------------------


def test_monitor_displays_native_statuses_and_title_fallback(
    client: TestClient, db_session: Session, seed_conversion_job, seed_source
) -> None:
    """Rows show the native status verbatim and fall back to the submit-time title."""
    source = seed_source(db_session, karakeep_id="bm_native", title=None)
    seed_conversion_job(
        db_session,
        source_id=source.source_id,
        idempotency_key="native-upload",
        status=ConversionJobStatus.UPLOAD_PENDING,
        title="Placeholder Title",
    )
    seed_conversion_job(
        db_session,
        source_id=source.source_id,
        idempotency_key="native-perm",
        status=ConversionJobStatus.FAILED_PERM,
        title="Placeholder Title",
    )

    response = client.get("/ui/tasks", params={"stage": "conversion"})

    assert response.status_code == 200
    body = response.text
    # Native conversion statuses, not the generic lifecycle vocabulary.
    assert '<span class="status UPLOAD_PENDING">UPLOAD_PENDING</span>' in body
    assert '<span class="status FAILED_PERM">FAILED_PERM</span>' in body
    # Source.title is NULL, so the row falls back to the submit-time job title.
    assert '<td class="title-cell">Placeholder Title</td>' in body


# --- KaraKeep search ----------------------------------------------------------


def test_search_by_karakeep_id_returns_the_job(
    client: TestClient, db_session: Session, seed_conversion_job, seed_source
) -> None:
    """The stage-declared KaraKeep identifier is matched through the generic search path."""
    matched = seed_source(db_session, karakeep_id="bm_findme", title="Findable Doc")
    other = seed_source(db_session, karakeep_id="bm_other", title="Other Doc")
    hit = seed_conversion_job(
        db_session, source_id=matched.source_id, idempotency_key="search-hit", status=ConversionJobStatus.QUEUED
    )
    miss = seed_conversion_job(
        db_session, source_id=other.source_id, idempotency_key="search-miss", status=ConversionJobStatus.QUEUED
    )

    response = client.get("/ui/tasks", params={"stage": "conversion", "search": "bm_findme"})

    assert response.status_code == 200
    assert f'<td class="mono">{hit.id}</td>' in response.text
    assert f'<td class="mono">{miss.id}</td>' not in response.text
    # And the KaraKeep column renders the bookmark id.
    assert "karakeep_id" in response.text
    assert "bm_findme" in response.text


# --- stage-native eligibility -------------------------------------------------


def test_cancel_skips_upload_pending_as_ineligible(
    client: TestClient, db_session: Session, seed_conversion_job, seed_source
) -> None:
    """Cancel is not eligible from ``UPLOAD_PENDING``; that job is skipped while a queued job cancels."""
    source = seed_source(db_session, karakeep_id="bm_cancel", title="Cancel Doc")
    queued = seed_conversion_job(
        db_session, source_id=source.source_id, idempotency_key="cancel-queued", status=ConversionJobStatus.QUEUED
    )
    uploading = seed_conversion_job(
        db_session,
        source_id=source.source_id,
        idempotency_key="cancel-upload",
        status=ConversionJobStatus.UPLOAD_PENDING,
    )

    response = client.post(
        "/ui/tasks/conversion/actions", data={"action": "cancel", "job_ids": [queued.id, uploading.id]}
    )

    assert response.status_code == 200
    assert "1 job cancelled" in response.text
    assert "1 skipped as ineligible" in response.text
    db_session.expire_all()
    assert db_session.get(ConversionJob, queued.id).status is ConversionJobStatus.CANCELLED
    assert db_session.get(ConversionJob, uploading.id).status is ConversionJobStatus.UPLOAD_PENDING


# --- action equivalence -------------------------------------------------------

_EVENT_FIELDS = ("stage", "kind", "from_status", "to_status", "attempt", "payload_json")


def _queued_event(session: Session, job_id: int) -> PipelineEvent:
    """Return the single conversion ``QUEUED`` event for a job."""
    events = session.exec(
        select(PipelineEvent)
        .where(PipelineEvent.stage == _CONVERSION_STAGE)
        .where(PipelineEvent.work_unit_ref == str(job_id))
        .where(PipelineEvent.to_status == ConversionJobStatus.QUEUED.value)
    ).all()
    assert len(events) == 1, f"expected exactly one QUEUED event, got {len(events)}"
    return events[0]


def test_console_retry_equals_the_shared_domain_retry(
    client: TestClient, db_session: Session, seed_conversion_job, seed_source
) -> None:
    """A console retry and a direct ``apply_job_retry`` reach the same state and requeue event.

    ``apply_job_retry`` is the shared domain helper the conversion JSON API and the
    worker call, so a console retry converging on it evidences one implementation.
    """
    source = seed_source(db_session, karakeep_id="bm_equiv", title="Equivalence Doc")
    via_console = seed_conversion_job(
        db_session,
        source_id=source.source_id,
        idempotency_key="equiv-console",
        status=ConversionJobStatus.FAILED_RETRYABLE,
        attempts=1,
    )
    via_helper = seed_conversion_job(
        db_session,
        source_id=source.source_id,
        idempotency_key="equiv-helper",
        status=ConversionJobStatus.FAILED_RETRYABLE,
        attempts=1,
    )

    response = client.post("/ui/tasks/conversion/actions", data={"action": "retry", "job_ids": [via_console.id]})
    assert response.status_code == 200

    apply_job_retry(db_session, via_helper, dt.datetime.now(dt.timezone.utc), submitted_by="self")
    db_session.commit()

    db_session.expire_all()
    console_job = db_session.get(ConversionJob, via_console.id)
    helper_job = db_session.get(ConversionJob, via_helper.id)
    # Same terminal status and the same cleared fields.
    assert console_job.status is ConversionJobStatus.QUEUED
    assert helper_job.status is ConversionJobStatus.QUEUED
    assert console_job.error_code is None and console_job.earliest_next_attempt_at is None
    assert console_job.attempts == helper_job.attempts == 2

    console_event = _queued_event(db_session, via_console.id)
    helper_event = _queued_event(db_session, via_helper.id)
    assert {f: getattr(console_event, f) for f in _EVENT_FIELDS} == {
        f: getattr(helper_event, f) for f in _EVENT_FIELDS
    }


# --- dashboard rollup ---------------------------------------------------------


def test_dashboard_rollup_is_lossless_and_splits_failed(
    client: TestClient, db_session: Session, seed_conversion_job, seed_source
) -> None:
    """Every native status counts under one category; the total equals the job count; failed splits."""
    source = seed_source(db_session, karakeep_id="bm_roll", title="Rollup Doc")
    seeded = [
        ConversionJobStatus.NEW,
        ConversionJobStatus.QUEUED,
        ConversionJobStatus.RUNNING,
        ConversionJobStatus.UPLOAD_PENDING,
        ConversionJobStatus.SUCCEEDED,
        ConversionJobStatus.FAILED_RETRYABLE,
        ConversionJobStatus.FAILED_PERM,
        ConversionJobStatus.FAILED_PERM,
        ConversionJobStatus.CANCELLED,
    ]
    for index, status in enumerate(seeded):
        seed_conversion_job(db_session, source_id=source.source_id, idempotency_key=f"roll-{index}", status=status)

    response = client.get("/ui")

    assert response.status_code == 200
    row = _dashboard_row(response.text, "conversion")
    # queued = NEW + QUEUED; running = RUNNING + UPLOAD_PENDING; failed = both variants.
    assert re.search(r'class="count queued"[^>]*>\s*2', row)
    assert re.search(r'class="count running"[^>]*>\s*2', row)
    assert re.search(r'class="count succeeded"[^>]*>\s*1', row)
    assert re.search(r'class="count cancelled"[^>]*>\s*1', row)
    # FAILED_RETRYABLE is awaiting-retry, both FAILED_PERM are permanent.
    assert 'data-awaiting-retry="1"' in row
    assert 'data-permanent="2"' in row
    # Nothing dropped or double-counted: total equals the 9 seeded jobs.
    assert '<td class="count total">9</td>' in row


# --- principal scoping --------------------------------------------------------


def test_foreign_owner_jobs_are_invisible_across_console_surfaces(
    client: TestClient, db_session: Session, seed_conversion_job, seed_source
) -> None:
    """A job owned by another principal is absent from the listing, counts, and drill-down."""
    source = seed_source(db_session, karakeep_id="bm_scope", title="Scope Doc")
    mine = seed_conversion_job(
        db_session, source_id=source.source_id, idempotency_key="scope-mine", status=ConversionJobStatus.QUEUED
    )
    theirs = seed_conversion_job(
        db_session,
        source_id=source.source_id,
        idempotency_key="scope-theirs",
        status=ConversionJobStatus.QUEUED,
        owner_id="other-principal",
    )

    # Monitor listing excludes the foreign job.
    monitor = client.get("/ui/tasks", params={"stage": "conversion"})
    assert f'<td class="mono">{mine.id}</td>' in monitor.text
    assert f'<td class="mono">{theirs.id}</td>' not in monitor.text

    # Dashboard counts only the owner's job.
    dashboard = client.get("/ui")
    assert '<td class="count total">1</td>' in _dashboard_row(dashboard.text, "conversion")

    # The foreign job's drill-down is not-found.
    assert client.get(f"/ui/tasks/conversion/{theirs.id}").status_code == 404


def test_bulk_action_reports_foreign_job_as_not_found_without_failing(
    client: TestClient, db_session: Session, seed_conversion_job, seed_source
) -> None:
    """A bulk action spanning a foreign job applies to the owner's and reports the other not-found."""
    source = seed_source(db_session, karakeep_id="bm_scopebulk", title="Scope Bulk Doc")
    mine = seed_conversion_job(
        db_session,
        source_id=source.source_id,
        idempotency_key="bulk-mine",
        status=ConversionJobStatus.FAILED_RETRYABLE,
    )
    theirs = seed_conversion_job(
        db_session,
        source_id=source.source_id,
        idempotency_key="bulk-theirs",
        status=ConversionJobStatus.FAILED_RETRYABLE,
        owner_id="other-principal",
    )

    response = client.post("/ui/tasks/conversion/actions", data={"action": "retry", "job_ids": [mine.id, theirs.id]})

    assert response.status_code == 200
    assert "1 job retried" in response.text
    assert "1 not found" in response.text
    db_session.expire_all()
    assert db_session.get(ConversionJob, mine.id).status is ConversionJobStatus.QUEUED
    # The foreign job is untouched (owner-scoping refused it, batch did not fail).
    assert db_session.get(ConversionJob, theirs.id).status is ConversionJobStatus.FAILED_RETRYABLE


# --- import boundary ----------------------------------------------------------


def test_conversion_descriptor_imports_only_conversion_domain_modules() -> None:
    """The descriptor imports only conversion domain code — no route, wiring, or processing."""
    tree = ast.parse(pathlib.Path(conversion_stage.__file__).read_text())
    conversion_imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("aizk.conversion"):
            conversion_imports.add(node.module)
        elif isinstance(node, ast.Import):
            conversion_imports.update(alias.name for alias in node.names if alias.name.startswith("aizk.conversion"))

    # Domain data/command modules plus the request-boundary Principal type; the
    # route, wiring, and processing packages are the off-limits ones.
    allowed = (
        "aizk.conversion.datamodel",
        "aizk.conversion.queries",
        "aizk.conversion.job_actions",
        "aizk.conversion.auth",
    )
    off_limits = {module for module in conversion_imports if not module.startswith(allowed)}
    assert not off_limits, f"conversion descriptor imports off-limits modules: {off_limits}"
    assert not any(
        module.startswith(("aizk.conversion.api", "aizk.conversion.processing")) for module in conversion_imports
    )
