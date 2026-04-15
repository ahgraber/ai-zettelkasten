"""Tests for the doc loader + disk cache (hermetic: in-memory SQLite, fake S3)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from _claimify import io as claimify_io
from _claimify.io import resolve_doc
import pytest
from sqlmodel import Session, SQLModel, create_engine

from aizk.conversion.datamodel.bookmark import Bookmark
from aizk.conversion.datamodel.output import ConversionOutput


class _FakeS3Client:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.calls: list[str] = []

    def get_object_bytes(self, key: str) -> bytes:
        self.calls.append(key)
        return self.payload


def _seed_db():
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    aizk_uuid = uuid4()
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        session.add(Bookmark(karakeep_id="kk-1", aizk_uuid=aizk_uuid, title="Doc"))
        session.add(
            ConversionOutput(
                job_id=1,
                aizk_uuid=aizk_uuid,
                title="Old title",
                payload_version=1,
                s3_prefix="pfx/",
                markdown_key="old.md",
                manifest_key="old-manifest.json",
                markdown_hash_xx64="0" * 16,
                docling_version="x",
                pipeline_name="p",
                created_at=now - timedelta(hours=1),
            )
        )
        session.add(
            ConversionOutput(
                job_id=2,
                aizk_uuid=aizk_uuid,
                title="New title",
                payload_version=1,
                s3_prefix="pfx/",
                markdown_key="new.md",
                manifest_key="new-manifest.json",
                markdown_hash_xx64="1" * 16,
                docling_version="x",
                pipeline_name="p",
                created_at=now,
            )
        )
        session.commit()
    return engine, aizk_uuid


def test_resolve_doc_picks_newest_and_caches(tmp_path, monkeypatch):
    monkeypatch.setattr(claimify_io, "CACHE_DIR", tmp_path)
    engine, aizk_uuid = _seed_db()
    s3 = _FakeS3Client(b"# New body")

    with Session(engine) as session:
        first = resolve_doc("kk-1", session, s3_client=s3)
        second = resolve_doc("kk-1", session, s3_client=s3)

    assert first.title == "New title"
    assert first.markdown == "# New body"
    assert first.source == "s3"
    assert s3.calls == ["new.md"]

    assert second.source == "cache"
    assert second.markdown == "# New body"
    assert s3.calls == ["new.md"]

    assert (tmp_path / f"{aizk_uuid}.md").read_text(encoding="utf-8") == "# New body"


def test_resolve_doc_missing_bookmark_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(claimify_io, "CACHE_DIR", tmp_path)
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)

    with Session(engine) as session, pytest.raises(ValueError, match="No bookmark"):
        resolve_doc("missing", session, s3_client=_FakeS3Client(b""))


def test_resolve_doc_missing_output_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(claimify_io, "CACHE_DIR", tmp_path)
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Bookmark(karakeep_id="kk-orphan", aizk_uuid=uuid4(), title="No outputs"))
        session.commit()
        with pytest.raises(ValueError, match="No conversion_outputs"):
            resolve_doc("kk-orphan", session, s3_client=_FakeS3Client(b""))
