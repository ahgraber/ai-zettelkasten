"""Tests for the doc loader + disk cache (hermetic: in-memory SQLite, fake S3)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from _claimify import io as claimify_io
from _claimify.io import resolve_doc
import pytest
from sqlmodel import Session, SQLModel, create_engine

from aizk.conversion.core.source_ref import KarakeepBookmarkRef, compute_source_ref_hash
from aizk.conversion.datamodel.output import ConversionOutput
from aizk.conversion.datamodel.source import Source


def _make_source(*, karakeep_id: str, source_id, title: str) -> Source:
    """Build a persistable Source row with a valid karakeep source_ref.

    `source_ref`, `source_ref_hash`, and `owner_id` are NOT NULL on the current
    schema; derive the ref/hash from the karakeep id and default the owner to the
    shipped principal so the row inserts cleanly.
    """
    ref = KarakeepBookmarkRef(bookmark_id=karakeep_id)
    return Source(
        karakeep_id=karakeep_id,
        source_id=source_id,
        title=title,
        source_ref=ref.model_dump_json(),
        source_ref_hash=compute_source_ref_hash(ref),
        owner_id="self",
    )


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
    source_id = uuid4()
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        session.add(_make_source(karakeep_id="kk-1", source_id=source_id, title="Doc"))
        session.add(
            ConversionOutput(
                job_id=1,
                source_id=source_id,
                owner_id="self",
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
                source_id=source_id,
                owner_id="self",
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
    return engine, source_id


def test_resolve_doc_picks_newest_and_caches(tmp_path, monkeypatch):
    monkeypatch.setattr(claimify_io, "CACHE_DIR", tmp_path)
    engine, source_id = _seed_db()
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

    assert (tmp_path / f"{source_id}.md").read_text(encoding="utf-8") == "# New body"


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
        session.add(_make_source(karakeep_id="kk-orphan", source_id=uuid4(), title="No outputs"))
        session.commit()
        with pytest.raises(ValueError, match="No conversion_outputs"):
            resolve_doc("kk-orphan", session, s3_client=_FakeS3Client(b""))
