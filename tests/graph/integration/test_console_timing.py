"""Seeded timing tests for the console task monitor.

The monitor's list and count queries group over the existing indexed status
columns, so a large unit table renders a page quickly and a realistic bulk action
reports within the operator-facing time bounds. The per-row-query (N+1) regression
these guard against is asserted deterministically by counting executed statements
(constant regardless of row count); the wall-clock scenarios from the spec are kept
as generous smoke bounds so they satisfy the "within N seconds" requirement without
being the sole, flake-prone guard.
"""

from __future__ import annotations

from collections.abc import Iterator
import contextlib
import time

from sqlalchemy import Engine, event
from sqlmodel import Session

from fastapi.testclient import TestClient

from aizk.graph.datamodel import ContextualizationJob
from aizk.pipeline.lifecycle import WorkUnitStatus


@contextlib.contextmanager
def _count_statements(engine: Engine) -> Iterator[list[int]]:
    """Count SQL statements executed on ``engine`` within the block.

    The result list holds a single running counter, so an N+1 (per-row-query)
    regression shows up as a count that scales with the row count instead of
    staying constant.
    """
    counter = [0]

    def _on_execute(*_args: object, **_kwargs: object) -> None:
        counter[0] += 1

    event.listen(engine, "after_cursor_execute", _on_execute)
    try:
        yield counter
    finally:
        event.remove(engine, "after_cursor_execute", _on_execute)


def _seed_jobs(session: Session, source_id, count: int, status: WorkUnitStatus) -> list[int]:
    """Bulk-insert ``count`` contextualization units for one source and return their ids."""
    jobs = [
        ContextualizationJob(
            idempotency_key=f"perf:{status.value}:{index}",
            conversion_output_id=index,
            source_id=source_id,
            status=status,
            attempts=0,
        )
        for index in range(count)
    ]
    session.add_all(jobs)
    session.commit()
    return [job.id for job in jobs]


def test_monitor_page_renders_within_two_seconds(
    client: TestClient, db_session: Session, seed_source
) -> None:
    """A 1000-unit stage monitor page renders within two seconds."""
    source = seed_source(db_session, karakeep_id="bm_perf_page", title="Perf Page Doc")
    _seed_jobs(db_session, source.source_id, 1000, WorkUnitStatus.QUEUED)

    start = time.perf_counter()
    response = client.get("/ui/tasks", params={"stage": "contextualization", "limit": 50})
    elapsed = time.perf_counter() - start

    assert response.status_code == 200
    assert "of 1000" in response.text
    assert elapsed < 2.0


def test_bulk_action_reports_within_five_seconds(
    client: TestClient, db_session: Session, seed_source
) -> None:
    """A bulk retry over a realistic (cap-sized) selection reports within five seconds."""
    source = seed_source(db_session, karakeep_id="bm_perf_bulk", title="Perf Bulk Doc")
    ids = _seed_jobs(db_session, source.source_id, 100, WorkUnitStatus.FAILED)

    start = time.perf_counter()
    response = client.post(
        "/ui/tasks/contextualization/actions", data={"action": "retry", "job_ids": ids}
    )
    elapsed = time.perf_counter() - start

    assert response.status_code == 200
    assert "100 jobs retried" in response.text
    assert elapsed < 5.0


def test_monitor_page_query_count_is_bounded_regardless_of_rows(
    client: TestClient, db_session: Session, seed_source, migrated_engine: Engine
) -> None:
    """Rendering a page issues a small, constant number of statements, not one per row.

    Deterministic guard against an N+1 regression: the monitor does a base count, a
    filtered count, and one page SELECT — a handful of statements — whether the
    table holds 1000 rows or a few.
    """
    source = seed_source(db_session, karakeep_id="bm_qcount", title="Query Count Doc")
    _seed_jobs(db_session, source.source_id, 1000, WorkUnitStatus.QUEUED)

    with _count_statements(migrated_engine) as counter:
        response = client.get("/ui/tasks", params={"stage": "contextualization", "limit": 50})

    assert response.status_code == 200
    # 3 expected (base count, filtered count, page select); allow headroom for
    # framework statements but far below the ~1000 an N+1 would produce.
    assert counter[0] <= 6, f"page render issued {counter[0]} statements — possible per-row query regression"
