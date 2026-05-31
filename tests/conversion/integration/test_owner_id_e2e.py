"""End-to-end validation for the deployment trust model.

Composes the unit and per-layer integration coverage into observable end-to-end
behaviours that an operator would care about:

- Full pipeline: a `trust_network` submission flows through API → worker →
  upload such that all three persisted rows carry the configured
  `AIZK_DEFAULT_PRINCIPAL`.
- API process refuses to start with a reserved-but-unimplemented auth mode
  (`AIZK_AUTH_MODE=token`).
- A fresh-clone deployment (default settings) accepts `Host: localhost` and
  `Host: 127.0.0.1` requests on `/health/live`.
- An operator override of `AIZK_TRUSTED_HOSTS` removes the localhost default,
  so a `Host: localhost` request returns HTTP 400.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlmodel import Session, select

from fastapi.testclient import TestClient

from aizk.conversion.api.main import create_app
from aizk.conversion.core.errors import ConfigurationError
from aizk.conversion.datamodel.job import ConversionJob
from aizk.conversion.datamodel.output import ConversionOutput
from aizk.conversion.datamodel.source import Source
from aizk.conversion.processing import uploader
from aizk.conversion.utilities.config import ConversionConfig


class _MockS3Client:
    bucket = "test-bucket"

    def __init__(self, *_args, **_kwargs) -> None:
        self.calls: list[str] = []

    def upload_file(self, _local_path, s3_key):
        self.calls.append(s3_key)
        return f"s3://test-bucket/{s3_key}"

    def upload_fileobj(self, _file_obj, s3_key):
        self.calls.append(s3_key)
        return f"s3://test-bucket/{s3_key}"


def _write_workspace(tmp_path: Path, *, bookmark_id: str, markdown_hash: str) -> Path:
    (tmp_path / "output.md").write_text("# E2E owner_id\n")
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


def test_full_pipeline_propagates_default_principal_to_all_three_tables(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
    tmp_path: Path,
) -> None:
    """API submission → worker upload → all three rows carry AIZK_DEFAULT_PRINCIPAL."""
    monkeypatch.setenv("AIZK_DEFAULT_PRINCIPAL", "deployment-owner")
    monkeypatch.setattr(uploader, "get_engine", lambda _url=None: db_session.get_bind())
    monkeypatch.setattr(uploader, "S3Client", _MockS3Client)

    bookmark_id = "bm_e2e_owner"
    app = create_app()
    with TestClient(app) as client:
        resp = client.post(
            "/v1/jobs",
            json={"source_ref": {"kind": "karakeep_bookmark", "bookmark_id": bookmark_id}},
        )
    assert resp.status_code == 201

    source = db_session.exec(select(Source).where(Source.karakeep_id == bookmark_id)).one()
    job = db_session.exec(select(ConversionJob).where(ConversionJob.aizk_uuid == source.aizk_uuid)).one()
    assert source.owner_id == "deployment-owner"
    assert job.owner_id == "deployment-owner"

    workspace = _write_workspace(tmp_path, bookmark_id=bookmark_id, markdown_hash="e2e0deadbeef0001")
    config = ConversionConfig(_env_file=None)
    uploader._upload_converted(job.id, workspace, config)

    db_session.expire_all()
    output = db_session.exec(select(ConversionOutput).where(ConversionOutput.job_id == job.id)).one()
    assert output.owner_id == "deployment-owner"


def test_api_refuses_to_start_with_reserved_auth_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """`AIZK_AUTH_MODE=token` raises ConfigurationError during lifespan; the listener never binds.

    Faithfully exercises the same control path as a subprocess launch — the
    typed startup error is what the supervising process sees as a non-zero
    exit — without the subprocess overhead. AuthSettings construction inside
    `lifespan` is the failure site.
    """
    monkeypatch.setenv("AIZK_AUTH_MODE", "token")
    app = create_app()
    with pytest.raises(ConfigurationError, match="not implemented at this cutover"), TestClient(app):
        pass


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1"])
def test_default_trusted_hosts_accept_loopback_on_health_live(monkeypatch: pytest.MonkeyPatch, host: str) -> None:
    """Fresh-clone defaults accept localhost and 127.0.0.1 on /health/live."""
    monkeypatch.delenv("AIZK_TRUSTED_HOSTS", raising=False)
    app = create_app()
    with TestClient(app, base_url=f"http://{host}") as client:
        resp = client.get("/health/live")
    assert resp.status_code == 200


def test_operator_override_removes_localhost_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit `AIZK_TRUSTED_HOSTS` override drops the shipped localhost default."""
    monkeypatch.setenv("AIZK_TRUSTED_HOSTS", '["api.example.internal"]')
    app = create_app()
    with TestClient(app, base_url="http://localhost") as client:
        resp = client.get("/health/live")
    assert resp.status_code == 400
    assert resp.text == "Invalid host header"
