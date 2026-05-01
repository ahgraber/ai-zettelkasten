"""Integration tests for the bookmark outputs endpoint."""

from __future__ import annotations

import datetime as dt
from uuid import UUID

import pytest

from fastapi.testclient import TestClient

from aizk.conversion.api.main import create_app
from aizk.conversion.datamodel.job import ConversionJobStatus
from aizk.conversion.datamodel.output import ConversionOutput
from tests.conversion._helpers import make_job, make_source


def _create_output(
    session,
    *,
    job_id: int,
    aizk_uuid: UUID,
    created_at: dt.datetime,
    markdown_hash: str,
    owner_id: str = "self",
) -> ConversionOutput:
    output = ConversionOutput(
        job_id=job_id,
        aizk_uuid=aizk_uuid,
        owner_id=owner_id,
        title="Test Output",
        payload_version=1,
        s3_prefix="s3://bucket/prefix",
        markdown_key="prefix/doc.md",
        manifest_key="prefix/manifest.json",
        markdown_hash_xx64=markdown_hash,
        figure_count=0,
        docling_version="1.0.0",
        pipeline_name="default",
        created_at=created_at,
    )
    session.add(output)
    session.commit()
    session.refresh(output)
    return output


@pytest.fixture()
def app():
    return create_app()


def test_get_bookmark_outputs_returns_all_ordered_descending(db_session, app) -> None:
    bookmark = make_source(db_session, "bm_outputs_all")
    job1 = make_job(
        db_session,
        aizk_uuid=bookmark.aizk_uuid,
        idempotency_key="a" * 64,
        status=ConversionJobStatus.SUCCEEDED,
        attempts=1,
    )
    job2 = make_job(
        db_session,
        aizk_uuid=bookmark.aizk_uuid,
        idempotency_key="b" * 64,
        status=ConversionJobStatus.SUCCEEDED,
        attempts=1,
    )

    now = dt.datetime.now(dt.timezone.utc)
    older = now - dt.timedelta(hours=1)

    out1 = _create_output(
        db_session, job_id=job1.id, aizk_uuid=bookmark.aizk_uuid, created_at=older, markdown_hash="aaa"
    )
    out2 = _create_output(
        db_session, job_id=job2.id, aizk_uuid=bookmark.aizk_uuid, created_at=now, markdown_hash="bbb"
    )

    with TestClient(app) as client:
        response = client.get(f"/v1/bookmarks/{bookmark.aizk_uuid}/outputs")

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2
    assert data[0]["id"] == out2.id
    assert data[1]["id"] == out1.id


def test_get_bookmark_outputs_latest_returns_one(db_session, app) -> None:
    bookmark = make_source(db_session, "bm_outputs_latest")
    job1 = make_job(
        db_session,
        aizk_uuid=bookmark.aizk_uuid,
        idempotency_key="c" * 64,
        status=ConversionJobStatus.SUCCEEDED,
        attempts=1,
    )
    job2 = make_job(
        db_session,
        aizk_uuid=bookmark.aizk_uuid,
        idempotency_key="d" * 64,
        status=ConversionJobStatus.SUCCEEDED,
        attempts=1,
    )

    now = dt.datetime.now(dt.timezone.utc)
    older = now - dt.timedelta(hours=1)

    _create_output(db_session, job_id=job1.id, aizk_uuid=bookmark.aizk_uuid, created_at=older, markdown_hash="ccc")
    out2 = _create_output(
        db_session, job_id=job2.id, aizk_uuid=bookmark.aizk_uuid, created_at=now, markdown_hash="ddd"
    )

    with TestClient(app) as client:
        response = client.get(f"/v1/bookmarks/{bookmark.aizk_uuid}/outputs", params={"latest": "true"})

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["id"] == out2.id


def test_get_bookmark_outputs_returns_only_owned_when_source_is_shared(db_session, app) -> None:
    """Shared aizk_uuid: caller sees only outputs whose owner_id matches the principal."""
    bookmark = make_source(db_session, "bm_outputs_owner_scope")
    owned_job = make_job(
        db_session,
        aizk_uuid=bookmark.aizk_uuid,
        idempotency_key="e" * 64,
        status=ConversionJobStatus.SUCCEEDED,
        attempts=1,
        owner_id="self",
    )
    cross_job = make_job(
        db_session,
        aizk_uuid=bookmark.aizk_uuid,
        idempotency_key="f" * 64,
        status=ConversionJobStatus.SUCCEEDED,
        attempts=1,
        owner_id="someone_else",
    )
    now = dt.datetime.now(dt.timezone.utc)
    owned_out = _create_output(
        db_session, job_id=owned_job.id, aizk_uuid=bookmark.aizk_uuid, created_at=now, markdown_hash="own"
    )
    _create_output(
        db_session,
        job_id=cross_job.id,
        aizk_uuid=bookmark.aizk_uuid,
        created_at=now,
        markdown_hash="cro",
        owner_id="someone_else",
    )

    with TestClient(app) as client:
        response = client.get(f"/v1/bookmarks/{bookmark.aizk_uuid}/outputs")

    assert response.status_code == 200
    data = response.json()
    assert [o["id"] for o in data] == [owned_out.id]


def test_get_bookmark_outputs_returns_empty_when_all_cross_owner(db_session, app) -> None:
    """All outputs owned by another principal: caller sees an empty list, not the rows."""
    bookmark = make_source(db_session, "bm_outputs_only_cross_owner")
    cross_job = make_job(
        db_session,
        aizk_uuid=bookmark.aizk_uuid,
        idempotency_key="g" * 64,
        status=ConversionJobStatus.SUCCEEDED,
        attempts=1,
        owner_id="someone_else",
    )
    now = dt.datetime.now(dt.timezone.utc)
    _create_output(
        db_session,
        job_id=cross_job.id,
        aizk_uuid=bookmark.aizk_uuid,
        created_at=now,
        markdown_hash="zzz",
        owner_id="someone_else",
    )

    with TestClient(app) as client:
        response = client.get(f"/v1/bookmarks/{bookmark.aizk_uuid}/outputs")

    assert response.status_code == 200
    assert response.json() == []


def test_get_bookmark_outputs_empty_for_unknown_uuid(app) -> None:
    unknown_uuid = "00000000-0000-0000-0000-000000000000"
    with TestClient(app) as client:
        response = client.get(f"/v1/bookmarks/{unknown_uuid}/outputs")

    assert response.status_code == 200
    assert response.json() == []
