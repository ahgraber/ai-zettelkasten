"""Integration tests for workers.source: _write_source_enrichment.

Covers: missing-row warning, db-exception-does-not-propagate, source_type /
terminal_ref mapping, and identity-immutability after enrichment.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch
import uuid

import pytest
from sqlmodel import Session

from aizk.conversion.core.source_ref import (
    ArxivRef,
    GithubReadmeRef,
    InlineHtmlRef,
    KarakeepBookmarkRef,
    UrlRef,
    compute_source_ref_hash,
)
from aizk.conversion.core.types import SOURCE_TYPE_BY_KIND
from aizk.conversion.datamodel.source import Source as Bookmark
from aizk.conversion.processing.source import _write_source_enrichment
from aizk.conversion.processing.types import SourceMetaFields, SubprocessMetadata


def _create_source_for_enrichment(db_session: Session, *, bookmark_id: str) -> Bookmark:
    ref = KarakeepBookmarkRef(kind="karakeep_bookmark", bookmark_id=bookmark_id)
    src = Bookmark(
        owner_id="self",
        karakeep_id=bookmark_id,
        source_ref=ref.model_dump_json(),
        source_ref_hash=compute_source_ref_hash(ref),
        url="https://example.com",
        normalized_url="https://example.com",
        title="Enrichment Test",
        content_type="html",
        source_type="web",
    )
    db_session.add(src)
    db_session.commit()
    db_session.refresh(src)
    return src


def _make_subprocess_meta(terminal_ref, content_type: str = "html") -> SubprocessMetadata:
    """Build a minimal SubprocessMetadata for enrichment tests."""
    return SubprocessMetadata(
        pipeline_name="html",
        terminal_ref=terminal_ref.model_dump(),
        content_type=content_type,
        markdown_filename="output.md",
        figure_files=[],
        markdown_hash_xx64="abc123def456789a",
        docling_version="test",
        config_snapshot={"converter_name": "docling"},
        fetched_at="2026-01-01T00:00:00+00:00",
        source_meta=SourceMetaFields(),
        document_title=None,
        source_title=None,
    )


class TestSourceEnrichment:
    """Tests for _write_source_enrichment: identity immutability, best-effort, source_type mapping."""

    def test_identity_columns_unchanged_after_enrichment(self, db_session):
        source = _create_source_for_enrichment(db_session, bookmark_id="bm_identity_imm")
        pre = {
            "source_id": source.source_id,
            "source_ref": source.source_ref,
            "source_ref_hash": source.source_ref_hash,
            "karakeep_id": source.karakeep_id,
        }
        terminal_ref = KarakeepBookmarkRef(kind="karakeep_bookmark", bookmark_id="bm_identity_imm")
        subprocess_meta = _make_subprocess_meta(terminal_ref, "html")

        _write_source_enrichment(
            subprocess_meta,
            str(source.source_id),
            db_session.get_bind(),
            job_id=0,  # synthetic — these tests don't have a job; FK is not enforced.
            attempt=1,
        )

        db_session.refresh(source)
        assert source.source_id == pre["source_id"]
        assert source.source_ref == pre["source_ref"]
        assert source.source_ref_hash == pre["source_ref_hash"]
        assert source.karakeep_id == pre["karakeep_id"]

    def test_enrichment_writes_mutable_metadata(self, db_session):
        source = _create_source_for_enrichment(db_session, bookmark_id="bm_mutable_enrich")
        terminal_ref = ArxivRef(kind="arxiv", arxiv_id="2401.00001")
        subprocess_meta = _make_subprocess_meta(terminal_ref, "pdf")

        _write_source_enrichment(
            subprocess_meta,
            str(source.source_id),
            db_session.get_bind(),
            job_id=0,
            attempt=1,
        )

        db_session.refresh(source)
        assert source.source_type == SOURCE_TYPE_BY_KIND["arxiv"]
        assert source.content_type == "pdf"

    def test_missing_source_row_logs_warning_and_does_not_raise(self, db_session, caplog):
        terminal_ref = KarakeepBookmarkRef(kind="karakeep_bookmark", bookmark_id="bm_missing")
        missing_uuid = uuid.UUID("00000000-0000-0000-0000-000000000001")
        subprocess_meta = _make_subprocess_meta(terminal_ref, "html")

        with caplog.at_level(logging.WARNING, logger="aizk.conversion.processing.source"):
            _write_source_enrichment(
                subprocess_meta,
                str(missing_uuid),
                db_session.get_bind(),
                job_id=0,
                attempt=1,
            )

        assert any("not found" in r.message.lower() or "enrichment" in r.message.lower() for r in caplog.records)

    def test_db_exception_does_not_propagate(self):
        terminal_ref = KarakeepBookmarkRef(kind="karakeep_bookmark", bookmark_id="bm_exc")
        subprocess_meta = _make_subprocess_meta(terminal_ref, "html")
        with patch("aizk.conversion.processing.source.Session", side_effect=RuntimeError("boom")):
            _write_source_enrichment(
                subprocess_meta,
                str(uuid.UUID("00000000-0000-0000-0000-000000000001")),
                MagicMock(),
                job_id=0,
                attempt=1,
            )


@pytest.mark.parametrize(
    "terminal_ref,expected_source_type",
    [
        (ArxivRef(kind="arxiv", arxiv_id="2401.00001"), SOURCE_TYPE_BY_KIND["arxiv"]),
        (
            GithubReadmeRef(kind="github_readme", owner="owner", repo="repo"),
            SOURCE_TYPE_BY_KIND["github_readme"],
        ),
        (UrlRef(kind="url", url="https://example.com"), SOURCE_TYPE_BY_KIND["url"]),
        (KarakeepBookmarkRef(kind="karakeep_bookmark", bookmark_id="bm_1"), SOURCE_TYPE_BY_KIND["karakeep_bookmark"]),
        (InlineHtmlRef(kind="inline_html", body=b"<html/>"), SOURCE_TYPE_BY_KIND["inline_html"]),
    ],
)
def test_source_type_set_from_terminal_ref_kind(terminal_ref, expected_source_type, db_session):
    """source_type is SOURCE_TYPE_BY_KIND[terminal_ref.kind] for every terminal kind."""
    bookmark_id = f"bm_srctype_{terminal_ref.kind}"
    source = _create_source_for_enrichment(db_session, bookmark_id=bookmark_id)
    subprocess_meta = _make_subprocess_meta(terminal_ref, "html")

    _write_source_enrichment(
        subprocess_meta,
        str(source.source_id),
        db_session.get_bind(),
        job_id=0,
        attempt=1,
    )

    db_session.refresh(source)
    assert source.source_type == expected_source_type
