"""Integration tests for owner_id propagation in the worker upload phase.

Pins the worker materialization contract from the deployment-trust-model change:

- The Output insert copies `owner_id` from the parent Job row.
- Worker NEVER mutates `Job.owner_id`.
- An impossible-after-migration NULL `Job.owner_id` raises `MissingOwnerOnJob`
  (defense-in-depth) and the Output row is NOT created.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlmodel import Session, select

from aizk.conversion.core.errors import MissingOwnerOnJob
from aizk.conversion.core.source_ref import KarakeepBookmarkRef, compute_source_ref_hash
from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.datamodel.output import ConversionOutput
from aizk.conversion.datamodel.source import Source
from aizk.conversion.processing import uploader
from aizk.conversion.utilities.config import ConversionConfig


def _make_workspace_metadata(tmp_path: Path, *, markdown_hash: str, bookmark_id: str) -> Path:
    """Write a minimal workspace with metadata.json + output.md."""
    import json

    (tmp_path / "output.md").write_text("# Owner propagation\n")
    metadata = {
        "markdown_filename": "output.md",
        "figure_files": [],
        "markdown_hash_xx64": markdown_hash,
        "docling_version": "1.0.0",
        "pipeline_name": "html",
        "content_type": "html",
        "fetched_at": "2026-04-29T00:00:00+00:00",
        "terminal_ref": {"kind": "karakeep_bookmark", "bookmark_id": bookmark_id},
        "config_snapshot": {
            "docling_pdf_max_pages": 250,
            "docling_enable_ocr": True,
            "docling_enable_table_structure": True,
            "docling_picture_description_model": "none",
            "docling_picture_timeout": 60.0,
            "docling_enable_picture_classification": True,
            "picture_description_enabled": False,
        },
        "source_meta": {},
        "document_title": None,
        "source_title": None,
    }
    (tmp_path / "metadata.json").write_text(json.dumps(metadata))
    return tmp_path


def _seed_source_and_job(
    db_session: Session,
    *,
    bookmark_id: str,
    owner_id: str = "bob",
) -> tuple[Source, ConversionJob]:
    ref = KarakeepBookmarkRef(bookmark_id=bookmark_id)
    source = Source(
        karakeep_id=bookmark_id,
        source_ref=ref.model_dump_json(),
        source_ref_hash=compute_source_ref_hash(ref),
        owner_id=owner_id,
        url="https://example.com",
        normalized_url="https://example.com",
        title=bookmark_id,
        content_type="html",
        source_type="web",
    )
    db_session.add(source)
    db_session.commit()
    db_session.refresh(source)

    job = ConversionJob(
        source_id=source.source_id,
        owner_id=owner_id,
        title=bookmark_id,
        idempotency_key=("o" * 64),
        status=ConversionJobStatus.RUNNING,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    return source, job


class _MockS3Client:
    """In-memory S3 stub: records calls, ignores payloads."""

    bucket = "test-bucket"

    def __init__(self, *_args, **_kwargs) -> None:
        self.calls: list[str] = []

    def upload_file(self, _local_path, s3_key):
        self.calls.append(s3_key)
        return f"s3://test-bucket/{s3_key}"

    def upload_fileobj(self, _file_obj, s3_key):
        self.calls.append(s3_key)
        return f"s3://test-bucket/{s3_key}"


def test_upload_propagates_owner_id_from_job_to_output(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
    tmp_path: Path,
) -> None:
    """A successful upload writes a ConversionOutput row carrying Job.owner_id."""
    monkeypatch.setattr(uploader, "get_engine", lambda _url=None: db_session.get_bind())
    monkeypatch.setattr(uploader, "S3Client", _MockS3Client)

    bookmark_id = "bm_owner_bob"
    _, job = _seed_source_and_job(db_session, bookmark_id=bookmark_id, owner_id="bob")
    workspace = _make_workspace_metadata(tmp_path, markdown_hash="dead0001beef0001", bookmark_id=bookmark_id)

    config = ConversionConfig(_env_file=None)
    uploader._upload_converted(job.id, workspace, config)

    db_session.expire_all()
    refreshed_job = db_session.get(ConversionJob, job.id)
    assert refreshed_job is not None
    assert refreshed_job.status == ConversionJobStatus.SUCCEEDED
    assert refreshed_job.owner_id == "bob", "worker must NOT mutate Job.owner_id"

    output = db_session.exec(select(ConversionOutput).where(ConversionOutput.job_id == job.id)).one()
    assert output.owner_id == "bob"


def test_upload_raises_missing_owner_when_job_owner_id_is_falsy(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
    tmp_path: Path,
) -> None:
    """An empty/missing ``Job.owner_id`` aborts the upload with ``MissingOwnerOnJob``; no Output is written.

    Post-migration the column is NOT NULL at the DB layer, so a literal NULL is
    unreachable via SQLite from a session that respects the constraint. The
    closest realistic degenerate state is a deployment with
    ``AIZK_DEFAULT_PRINCIPAL=""``; the defensive guard
    (``if not job.owner_id:``) covers both NULL and empty-string. We exercise
    the empty-string variant by force-UPDATEing the column. The Output row
    must NOT be created.
    """
    monkeypatch.setattr(uploader, "get_engine", lambda _url=None: db_session.get_bind())
    monkeypatch.setattr(uploader, "S3Client", _MockS3Client)

    bookmark_id = "bm_owner_falsy"
    _, job = _seed_source_and_job(db_session, bookmark_id=bookmark_id, owner_id="bob")
    job_id = job.id

    db_session.execute(text("UPDATE conversion_jobs SET owner_id = '' WHERE id = :jid"), {"jid": job_id})
    db_session.commit()
    db_session.expire_all()

    workspace = _make_workspace_metadata(tmp_path, markdown_hash="dead0002beef0002", bookmark_id=bookmark_id)

    config = ConversionConfig(_env_file=None)
    with pytest.raises(MissingOwnerOnJob, match="no owner_id"):
        uploader._upload_converted(job_id, workspace, config)

    outputs = db_session.exec(select(ConversionOutput).where(ConversionOutput.job_id == job_id)).all()
    assert outputs == []
