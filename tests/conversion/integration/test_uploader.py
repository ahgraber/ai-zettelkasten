"""Integration tests for the conversion uploader: content-hash dedup shortcut."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlmodel import Session

from aizk.conversion.core.source_ref import KarakeepBookmarkRef, compute_source_ref_hash
from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.datamodel.output import ConversionOutput
from aizk.conversion.datamodel.source import Source as Bookmark
from aizk.conversion.processing import uploader
from aizk.conversion.utilities.config import ConversionConfig


def _make_workspace_metadata(tmp_path: Path, *, markdown_hash: str) -> Path:
    """Write a minimal workspace with metadata.json and output.md."""
    (tmp_path / "output.md").write_text("# Content")
    metadata = {
        "markdown_filename": "output.md",
        "figure_files": [],
        "markdown_hash_xx64": markdown_hash,
        "docling_version": "1.0.0",
        "pipeline_name": "html",
        "content_type": "html",
        "fetched_at": "2026-01-01T00:00:00+00:00",
        "terminal_ref": {"kind": "karakeep_bookmark", "bookmark_id": "bm_workspace_default"},
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


def test_upload_converted_reuses_s3_when_hash_matches(monkeypatch, db_session: Session, tmp_path: Path) -> None:
    """When content hash matches a prior output, S3 upload is skipped and existing keys are reused."""
    monkeypatch.setattr(uploader, "get_engine", lambda _url=None: db_session.get_bind())

    _ref_reuse = KarakeepBookmarkRef(bookmark_id="bm_hash_reuse")
    bookmark = Bookmark(
        owner_id="self",
        karakeep_id="bm_hash_reuse",
        source_ref=_ref_reuse.model_dump_json(),
        source_ref_hash=compute_source_ref_hash(_ref_reuse),
        url="https://example.com",
        normalized_url="https://example.com",
        title="Hash Reuse",
        content_type="html",
        source_type="web",
    )
    db_session.add(bookmark)
    db_session.commit()
    db_session.refresh(bookmark)

    prior_job = ConversionJob(
        source_id=bookmark.source_id,
        owner_id="self",
        title="Hash Reuse",
        idempotency_key="p" * 64,
        status=ConversionJobStatus.SUCCEEDED,
    )
    db_session.add(prior_job)
    db_session.commit()
    db_session.refresh(prior_job)

    known_hash = "abc123def456789a"
    prior_output = ConversionOutput(
        owner_id="self",
        job_id=prior_job.id,
        source_id=bookmark.source_id,
        title="Hash Reuse",
        payload_version=1,
        s3_prefix=f"s3://bucket/{bookmark.source_id}/",
        markdown_key=f"{bookmark.source_id}/output.md",
        manifest_key=f"{bookmark.source_id}/manifest.json",
        markdown_hash_xx64=known_hash,
        figure_count=0,
        docling_version="1.0.0",
        pipeline_name="html",
    )
    db_session.add(prior_output)
    db_session.commit()

    new_job = ConversionJob(
        source_id=bookmark.source_id,
        owner_id="self",
        title="Hash Reuse",
        idempotency_key="n" * 64,
        status=ConversionJobStatus.RUNNING,
    )
    db_session.add(new_job)
    db_session.commit()
    db_session.refresh(new_job)

    workspace = _make_workspace_metadata(tmp_path, markdown_hash=known_hash)

    upload_calls: list[str] = []

    class _MockS3Client:
        bucket = "bucket"

        def upload_file(self, local_path, s3_key):
            upload_calls.append(s3_key)
            return f"s3://bucket/{s3_key}"

        def upload_fileobj(self, fileobj, s3_key):
            # Drain the file-like object to mimic the real client's read.
            fileobj.read()
            upload_calls.append(s3_key)
            return f"s3://bucket/{s3_key}"

    monkeypatch.setattr(uploader, "S3Client", lambda _config: _MockS3Client())

    config = ConversionConfig(_env_file=None)
    uploader._upload_converted(new_job.id, workspace, config)

    # Markdown + figures are content-addressed and reused, but the manifest is
    # always rewritten so the source block reflects THIS job's SubprocessMetadata.
    assert upload_calls == [prior_output.manifest_key], (
        "shortcut path should upload exactly one object (the regenerated manifest)"
    )

    db_session.refresh(new_job)
    assert new_job.status == ConversionJobStatus.SUCCEEDED

    from sqlmodel import select as _select

    outputs = db_session.exec(_select(ConversionOutput).where(ConversionOutput.job_id == new_job.id)).all()
    assert len(outputs) == 1
    assert outputs[0].markdown_key == prior_output.markdown_key
    assert outputs[0].s3_prefix == prior_output.s3_prefix
    assert outputs[0].markdown_hash_xx64 == known_hash


def test_upload_converted_shortcut_writes_fresh_manifest_with_current_source_meta(
    monkeypatch, db_session: Session, tmp_path: Path
) -> None:
    """Regression: dedup shortcut must write a fresh manifest reflecting THIS job's
    SubprocessMetadata, not reuse the prior manifest's source block.

    Per conversion-worker spec § "Persist conversion config and source provenance"
    and the universal scenario "Manifest values independent of Source-row state":
    manifest.source.{url,normalized_url,title} SHALL come from the current job's
    SubprocessMetadata for every write path, including the content-hash dedup
    shortcut. Reusing the prior manifest publishes stale source values for any
    job whose source metadata diverged from the prior conversion of the same content.
    """

    monkeypatch.setattr(uploader, "get_engine", lambda _url=None: db_session.get_bind())

    _ref_div = KarakeepBookmarkRef(bookmark_id="bm_div_meta")
    bookmark = Bookmark(
        owner_id="self",
        karakeep_id="bm_div_meta",
        source_ref=_ref_div.model_dump_json(),
        source_ref_hash=compute_source_ref_hash(_ref_div),
        url="https://example.com",
        normalized_url="https://example.com",
        title="Old Title",
        content_type="html",
        source_type="web",
    )
    db_session.add(bookmark)
    db_session.commit()
    db_session.refresh(bookmark)

    prior_job = ConversionJob(
        source_id=bookmark.source_id,
        owner_id="self",
        title="Old Title",
        idempotency_key="d" * 64,
        status=ConversionJobStatus.SUCCEEDED,
    )
    db_session.add(prior_job)
    db_session.commit()
    db_session.refresh(prior_job)

    known_hash = "fedcba9876543210"
    prior_output = ConversionOutput(
        owner_id="self",
        job_id=prior_job.id,
        source_id=bookmark.source_id,
        title="Old Title",
        payload_version=1,
        s3_prefix=f"s3://bucket/{bookmark.source_id}/",
        markdown_key=f"{bookmark.source_id}/output.md",
        manifest_key=f"{bookmark.source_id}/manifest.json",
        markdown_hash_xx64=known_hash,
        figure_count=0,
        docling_version="1.0.0",
        pipeline_name="html",
    )
    db_session.add(prior_output)
    db_session.commit()

    new_job = ConversionJob(
        source_id=bookmark.source_id,
        owner_id="self",
        title="placeholder-uuid-fallback",
        idempotency_key="e" * 64,
        status=ConversionJobStatus.RUNNING,
    )
    db_session.add(new_job)
    db_session.commit()
    db_session.refresh(new_job)

    # Workspace metadata for the NEW job carries new source values that diverge
    # from the prior conversion (e.g. user updated the bookmark title and URL
    # between conversions of identical content).
    (tmp_path / "output.md").write_text("# Content")
    new_source_url = "https://example.com/new-canonical-url"
    new_normalized = "https://example.com/new-canonical-url"
    new_source_title = "Brand-New Title"
    metadata = {
        "markdown_filename": "output.md",
        "figure_files": [],
        "markdown_hash_xx64": known_hash,
        "docling_version": "1.0.0",
        "pipeline_name": "html",
        "content_type": "html",
        "fetched_at": "2026-05-03T00:00:00+00:00",
        "terminal_ref": {"kind": "karakeep_bookmark", "bookmark_id": "bm_div_meta"},
        "config_snapshot": {
            "docling_pdf_max_pages": 250,
            "docling_enable_ocr": True,
            "docling_enable_table_structure": True,
            "docling_picture_description_model": "none",
            "docling_picture_timeout": 60.0,
            "docling_enable_picture_classification": True,
            "picture_description_enabled": False,
        },
        "source_meta": {
            "source_url": new_source_url,
            "normalized_url": new_normalized,
            "document_base_url": new_source_url,
            "resolver_title": new_source_title,
        },
        "document_title": new_source_title,
        "source_title": new_source_title,
    }
    (tmp_path / "metadata.json").write_text(json.dumps(metadata))

    captured: dict[str, bytes] = {}

    class _CapturingS3Client:
        bucket = "bucket"

        def upload_file(self, local_path, s3_key):
            return f"s3://bucket/{s3_key}"

        def upload_fileobj(self, fileobj, s3_key):
            captured[s3_key] = fileobj.read()
            return f"s3://bucket/{s3_key}"

    monkeypatch.setattr(uploader, "S3Client", lambda _config: _CapturingS3Client())

    config = ConversionConfig(_env_file=None)
    uploader._upload_converted(new_job.id, tmp_path, config)

    # The manifest at the shared S3 key must reflect the NEW job's source values.
    assert prior_output.manifest_key in captured, "shortcut path must upload a fresh manifest"
    written = json.loads(captured[prior_output.manifest_key].decode("utf-8"))
    assert written["source"]["url"] == new_source_url, (
        f"shortcut manifest source.url must come from THIS job's SubprocessMetadata, got {written['source']['url']!r}"
    )
    assert written["source"]["normalized_url"] == new_normalized
    assert written["source"]["title"] == new_source_title

    # ConversionOutput.title must also reflect the new source_title (not job placeholder).
    db_session.refresh(new_job)
    assert new_job.status == ConversionJobStatus.SUCCEEDED
    from sqlmodel import select as _select

    outputs = db_session.exec(_select(ConversionOutput).where(ConversionOutput.job_id == new_job.id)).all()
    assert len(outputs) == 1
    assert outputs[0].title == new_source_title
    # Markdown / figure storage is still reused from the prior output.
    assert outputs[0].markdown_key == prior_output.markdown_key
    assert outputs[0].s3_prefix == prior_output.s3_prefix


def test_upload_converted_shortcut_manifest_upload_is_retryable(
    monkeypatch, db_session: Session, tmp_path: Path
) -> None:
    """Regression: dedup shortcut's manifest upload must be retryable.

    The worker's upload retry policy wraps ``_execute_upload``, not
    ``_prepare_upload``. The shortcut path therefore must not perform any S3
    IO during prep — the manifest PUT must run inside ``_execute_upload`` so a
    transient S3 failure on attempt 1 is retried, not converted to a permanent
    job failure.
    """
    monkeypatch.setattr(uploader, "get_engine", lambda _url=None: db_session.get_bind())

    _ref = KarakeepBookmarkRef(bookmark_id="bm_retry_shortcut")
    bookmark = Bookmark(
        owner_id="self",
        karakeep_id="bm_retry_shortcut",
        source_ref=_ref.model_dump_json(),
        source_ref_hash=compute_source_ref_hash(_ref),
        url="https://example.com",
        normalized_url="https://example.com",
        title="Retry Shortcut",
        content_type="html",
        source_type="web",
    )
    db_session.add(bookmark)
    db_session.commit()
    db_session.refresh(bookmark)

    prior_job = ConversionJob(
        source_id=bookmark.source_id,
        owner_id="self",
        title="Retry Shortcut",
        idempotency_key="r" * 64,
        status=ConversionJobStatus.SUCCEEDED,
    )
    db_session.add(prior_job)
    db_session.commit()
    db_session.refresh(prior_job)

    known_hash = "0123456789abcdef"
    prior_output = ConversionOutput(
        owner_id="self",
        job_id=prior_job.id,
        source_id=bookmark.source_id,
        title="Retry Shortcut",
        payload_version=1,
        s3_prefix=f"s3://bucket/{bookmark.source_id}/",
        markdown_key=f"{bookmark.source_id}/output.md",
        manifest_key=f"{bookmark.source_id}/manifest.json",
        markdown_hash_xx64=known_hash,
        figure_count=0,
        docling_version="1.0.0",
        pipeline_name="html",
    )
    db_session.add(prior_output)
    db_session.commit()

    new_job = ConversionJob(
        source_id=bookmark.source_id,
        owner_id="self",
        title="Retry Shortcut",
        idempotency_key="s" * 64,
        status=ConversionJobStatus.RUNNING,
    )
    db_session.add(new_job)
    db_session.commit()
    db_session.refresh(new_job)

    workspace = _make_workspace_metadata(tmp_path, markdown_hash=known_hash)

    upload_attempts: list[str] = []
    fail_next = {"value": True}

    class _FlakeyS3Client:
        bucket = "bucket"

        def upload_file(self, local_path, s3_key):
            upload_attempts.append(s3_key)
            return f"s3://bucket/{s3_key}"

        def upload_fileobj(self, fileobj, s3_key):
            fileobj.read()
            upload_attempts.append(s3_key)
            if fail_next["value"]:
                fail_next["value"] = False
                from aizk.conversion.storage.s3_client import S3UploadError

                raise S3UploadError(s3_key, "transient failure for retry test")
            return f"s3://bucket/{s3_key}"

    monkeypatch.setattr(uploader, "S3Client", lambda _config: _FlakeyS3Client())

    config = ConversionConfig(_env_file=None)

    # Step 1: prepare must NOT touch S3 (retry contract: prep is one-shot).
    plan = uploader._prepare_upload(new_job.id, workspace, config)
    assert plan is not None, "shortcut should still return a plan so retry loop can run"
    assert plan.markdown_local is None, "shortcut plan must skip markdown upload"
    assert plan.figure_uploads == (), "shortcut plan must skip figure uploads"
    assert upload_attempts == [], "_prepare_upload must not perform any S3 PUTs"

    # Step 2: first execute attempt fails on the manifest PUT.
    from aizk.conversion.storage.s3_client import S3UploadError

    with pytest.raises(S3UploadError):
        uploader._execute_upload(plan, new_job.id, config)
    assert upload_attempts == [prior_output.manifest_key]

    # Step 3: retry succeeds — same plan, idempotent on the manifest key.
    uploader._execute_upload(plan, new_job.id, config)
    assert upload_attempts == [prior_output.manifest_key, prior_output.manifest_key]

    db_session.refresh(new_job)
    assert new_job.status == ConversionJobStatus.SUCCEEDED
    from sqlmodel import select as _select

    outputs = db_session.exec(_select(ConversionOutput).where(ConversionOutput.job_id == new_job.id)).all()
    assert len(outputs) == 1
    assert outputs[0].markdown_key == prior_output.markdown_key


def test_upload_converted_uploads_when_hash_differs(monkeypatch, db_session: Session, tmp_path: Path) -> None:
    """When no prior output has a matching hash, the full S3 upload proceeds."""
    monkeypatch.setattr(uploader, "get_engine", lambda _url=None: db_session.get_bind())

    _ref_upload = KarakeepBookmarkRef(bookmark_id="bm_hash_upload")
    bookmark = Bookmark(
        owner_id="self",
        karakeep_id="bm_hash_upload",
        source_ref=_ref_upload.model_dump_json(),
        source_ref_hash=compute_source_ref_hash(_ref_upload),
        url="https://example.com",
        normalized_url="https://example.com",
        title="Hash Upload",
        content_type="html",
        source_type="web",
    )
    db_session.add(bookmark)
    db_session.commit()
    db_session.refresh(bookmark)

    new_job = ConversionJob(
        source_id=bookmark.source_id,
        owner_id="self",
        title="Hash Upload",
        idempotency_key="u" * 64,
        status=ConversionJobStatus.RUNNING,
    )
    db_session.add(new_job)
    db_session.commit()
    db_session.refresh(new_job)

    workspace = _make_workspace_metadata(tmp_path, markdown_hash="newhash00000001a")

    upload_calls: list[str] = []

    class _MockS3Client:
        bucket = "test-bucket"

        def upload_file(self, local_path, s3_key):
            upload_calls.append(s3_key)
            return f"s3://test-bucket/{s3_key}"

        def upload_fileobj(self, file_obj, s3_key):
            # _upload_nofollow opens the path with O_NOFOLLOW and passes the
            # resulting file object through this entrypoint instead of upload_file.
            upload_calls.append(s3_key)
            return f"s3://test-bucket/{s3_key}"

    monkeypatch.setattr(uploader, "S3Client", lambda _config: _MockS3Client())

    config = ConversionConfig(_env_file=None)
    uploader._upload_converted(new_job.id, workspace, config)

    assert any("output.md" in key for key in upload_calls), "Markdown should be uploaded when no hash match"

    db_session.refresh(new_job)
    assert new_job.status == ConversionJobStatus.SUCCEEDED

    from sqlmodel import select as _select

    outputs = db_session.exec(_select(ConversionOutput).where(ConversionOutput.job_id == new_job.id)).all()
    assert len(outputs) == 1
    assert outputs[0].markdown_key == f"{bookmark.source_id}/output.md", "markdown_key must be a bare S3 key"
    assert outputs[0].manifest_key == f"{bookmark.source_id}/manifest.json", "manifest_key must be a bare S3 key"

