"""Integration tests for the bookmark outputs endpoint."""

from __future__ import annotations

import datetime as dt
from uuid import UUID

import pytest

from fastapi.testclient import TestClient

from aizk.conversion.api.main import create_app
from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.datamodel.output import ConversionOutput
from aizk.conversion.datamodel.source import Source as Bookmark


def _create_bookmark(session, karakeep_id: str) -> Bookmark:
    bookmark = Bookmark(karakeep_id=karakeep_id)
    session.add(bookmark)
    session.commit()
    session.refresh(bookmark)
    return bookmark


def _create_job(session, *, aizk_uuid: UUID, idempotency_key: str) -> ConversionJob:
    job = ConversionJob(
        aizk_uuid=aizk_uuid,
        title="Test",
        payload_version=1,
        status=ConversionJobStatus.SUCCEEDED,
        attempts=1,
        idempotency_key=idempotency_key,
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def _create_output(
    session,
    *,
    job_id: int,
    aizk_uuid: UUID,
    created_at: dt.datetime,
    markdown_hash: str,
) -> ConversionOutput:
    output = ConversionOutput(
        job_id=job_id,
        aizk_uuid=aizk_uuid,
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
    bookmark = _create_bookmark(db_session, "bm_outputs_all")
    job1 = _create_job(db_session, aizk_uuid=bookmark.aizk_uuid, idempotency_key="a" * 64)
    job2 = _create_job(db_session, aizk_uuid=bookmark.aizk_uuid, idempotency_key="b" * 64)

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
    bookmark = _create_bookmark(db_session, "bm_outputs_latest")
    job1 = _create_job(db_session, aizk_uuid=bookmark.aizk_uuid, idempotency_key="c" * 64)
    job2 = _create_job(db_session, aizk_uuid=bookmark.aizk_uuid, idempotency_key="d" * 64)

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


def test_get_bookmark_outputs_empty_for_unknown_uuid(app) -> None:
    unknown_uuid = "00000000-0000-0000-0000-000000000000"
    with TestClient(app) as client:
        response = client.get(f"/v1/bookmarks/{unknown_uuid}/outputs")

    assert response.status_code == 200
    assert response.json() == []
