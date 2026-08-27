"""Robustness behaviors of the console monitor and its one mutating route.

These pin the review-driven hardening of the console: a search term's ``LIKE``
metacharacters match literally rather than acting as wildcards; the monitor's
list total and the dashboard's count agree even for a work-unit whose source row is
absent (the join shapes are aligned); a delete emits a structured audit line (the
only durable trace of the event-less delete) and its console control carries a
confirmation guard; and the action route bounds its search input like the monitor.
"""

from __future__ import annotations

import logging
import re
from uuid import uuid4

from sqlmodel import Session

from fastapi.testclient import TestClient

from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.pipeline.lifecycle import WorkUnitStatus

# --- L1: LIKE metacharacters match literally ---------------------------------


def test_search_treats_underscore_as_a_literal_not_a_wildcard(
    client: TestClient, db_session: Session, seed_conversion_job, seed_source
) -> None:
    """A search containing ``_`` matches the character literally, not as a single-char wildcard."""
    literal = seed_source(db_session, karakeep_id="bm_underscore", title="a_c")
    wildcardish = seed_source(db_session, karakeep_id="bm_abc", title="abc")
    hit = seed_conversion_job(
        db_session, source_id=literal.source_id, idempotency_key="us-hit", status=ConversionJobStatus.QUEUED
    )
    miss = seed_conversion_job(
        db_session, source_id=wildcardish.source_id, idempotency_key="us-miss", status=ConversionJobStatus.QUEUED
    )

    response = client.get("/ui/tasks", params={"stage": "conversion", "search": "a_c"})

    assert response.status_code == 200
    # "a_c" matches the literal "a_c" title only; an unescaped "_" would also match "abc".
    assert f'<td class="mono">{hit.id}</td>' in response.text
    assert f'<td class="mono">{miss.id}</td>' not in response.text


# --- L4: monitor total and dashboard count agree for an orphan-source unit ----


def test_orphan_source_unit_lists_and_counts_consistently(
    client: TestClient, db_session: Session, seed_source, seed_contextualization_job
) -> None:
    """A graph unit whose source_id has no ``sources`` row still lists and is counted, consistently.

    Graph work-units carry no FK on ``source_id``, so an orphan is reachable; the
    monitor's outer join keeps it visible (source-id fallback title) and the
    dashboard's join-free count agrees with the monitor total.
    """
    present = seed_source(db_session, karakeep_id="bm_ctx_present", title="Present Doc")
    seed_contextualization_job(
        db_session, source_id=present.source_id, conversion_output_id=1, status=WorkUnitStatus.QUEUED
    )
    orphan_source_id = uuid4()  # no sources row for this id
    orphan = seed_contextualization_job(
        db_session, source_id=orphan_source_id, conversion_output_id=2, status=WorkUnitStatus.QUEUED
    )

    monitor = client.get("/ui/tasks", params={"stage": "contextualization"})
    dashboard = client.get("/ui")

    assert monitor.status_code == 200 and dashboard.status_code == 200
    # The orphan lists with its source-id fallback title (no source row to enrich).
    assert f'<td class="mono">{orphan.id}</td>' in monitor.text
    assert f'<td class="title-cell">{orphan_source_id}</td>' in monitor.text
    # Monitor total and dashboard total agree (2 units, none hidden by the join).
    assert "(2 total)" in monitor.text
    row = re.search(r'<tr data-stage="contextualization".*?</tr>', dashboard.text, re.DOTALL)
    assert row is not None
    assert '<td class="count total">2</td>' in row.group(0)


# --- L2 + L3: delete leaves a trace and its control confirms ------------------


def test_delete_emits_a_structured_audit_log(
    client: TestClient, db_session: Session, seed_conversion_job, seed_source, caplog
) -> None:
    """A delete records a structured audit line — the only durable trace of the event-less delete."""
    source = seed_source(db_session, karakeep_id="bm_auditlog", title="Audit Doc")
    job = seed_conversion_job(
        db_session, source_id=source.source_id, idempotency_key="audit-del", status=ConversionJobStatus.FAILED_PERM
    )

    with caplog.at_level(logging.INFO, logger="aizk.console.routes"):
        response = client.post("/ui/tasks/conversion/actions", data={"action": "delete", "job_ids": [job.id]})

    assert response.status_code == 200
    record = next((r for r in caplog.records if r.name == "aizk.console.routes"), None)
    assert record is not None, "expected a console action audit log record"
    assert record.action == "delete"
    assert record.stage == "conversion"
    assert record.applied == 1
    assert record.principal == "self"


def test_delete_control_carries_a_confirmation_guard(
    client: TestClient, db_session: Session, seed_conversion_job, seed_source
) -> None:
    """The conversion monitor's Delete button carries an htmx confirmation guard; Retry does not."""
    source = seed_source(db_session, karakeep_id="bm_confirm", title="Confirm Doc")
    seed_conversion_job(
        db_session, source_id=source.source_id, idempotency_key="confirm-job", status=ConversionJobStatus.FAILED_PERM
    )

    body = client.get("/ui/tasks", params={"stage": "conversion"}).text

    delete_button = re.search(r'value="delete"[^>]*>', body, re.DOTALL)
    assert delete_button is not None and "hx-confirm=" in delete_button.group(0)
    retry_button = re.search(r'value="retry"[^>]*>', body, re.DOTALL)
    assert retry_button is not None and "hx-confirm=" not in retry_button.group(0)


# --- L8: the action route bounds its search input like the monitor -----------


def test_action_route_rejects_an_overlong_search(client: TestClient) -> None:
    """The action route rejects a search over the 200-char bound (422), matching the monitor."""
    response = client.post(
        "/ui/tasks/conversion/actions",
        data={"action": "retry", "search": "x" * 201},
    )

    assert response.status_code == 422
