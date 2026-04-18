"""Integration test for end-to-end conversion flow."""

from __future__ import annotations

import datetime as dt
import hashlib
import queue as queue_module
from uuid import UUID

import boto3
from botocore.stub import ANY, Stubber
from sqlalchemy.orm import Mapped
from sqlmodel import Session, select

from fastapi.testclient import TestClient

from aizk.conversion.api.main import create_app
from aizk.conversion.datamodel.bookmark import Bookmark
from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.datamodel.output import ConversionOutput
from aizk.conversion.db import get_engine
from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.utilities.hashing import compute_idempotency_key
from aizk.conversion.workers import orchestrator, uploader
from aizk.conversion.workers.types import ConversionInput


class _InlineProcess:
    """Process that executes target immediately in the same process for testing."""

    def __init__(self, target, args) -> None:
        self._target = target
        self._args = args
        self.exitcode = None

    def start(self) -> None:
        try:
            self._target(*self._args)
            self.exitcode = 0
        except Exception:
            self.exitcode = 1

    def is_alive(self) -> bool:
        return False

    def join(self, timeout: float | None = None) -> None:
        return

    def terminate(self) -> None:
        return

    def kill(self) -> None:
        return


class _InlineContext:
    """Context that provides Queue and inline-executing Process for testing."""

    def Queue(self):  # noqa: N802
        return queue_module.Queue()

    def Process(self, target, args, daemon: bool):  # noqa: N802
        return _InlineProcess(target, args)


def test_conversion_flow_end_to_end(monkeypatch, html_bookmark):
    """Exercise API submit + worker processing with stubbed external services."""
    monkeypatch.setattr(orchestrator.mp, "get_context", lambda _ctx: _InlineContext())
    app = create_app()
    if not any(getattr(route, "path", None) == "/v1/jobs" for route in app.router.routes):
        raise AssertionError("Jobs routes not registered in FastAPI app yet.")

    # Stub S3 client to avoid network calls while still verifying upload behavior.
    s3_client = boto3.client("s3", region_name="us-east-1")
    stubber = Stubber(s3_client)
    stubber.activate()

    def _init_s3_client(self, config):
        # Inject the stubbed client into S3Client for deterministic uploads.
        self.config = config
        self.client = s3_client
        self.bucket = config.s3_bucket_name

    def _upload_file(self, local_path, s3_key: str) -> str:
        # Emulate upload + HEAD verification without touching real S3.
        body = local_path.read_bytes()
        stubber.add_response(
            "put_object",
            {},
            {"Bucket": self.bucket, "Key": s3_key, "Body": ANY},
        )
        self.client.put_object(Bucket=self.bucket, Key=s3_key, Body=body)

        md5_hash = hashlib.md5(body).hexdigest()  # NOQA: S324
        stubber.add_response(
            "head_object",
            {"ETag": f'"{md5_hash}"', "ContentLength": len(body)},
            {"Bucket": self.bucket, "Key": s3_key},
        )
        self.client.head_object(Bucket=self.bucket, Key=s3_key)
        return f"s3://{self.bucket}/{s3_key}"

    # Avoid KaraKeep network calls; reuse a fixed bookmark payload.
    monkeypatch.setattr(
        "aizk.conversion.workers.orchestrator.fetch_karakeep_bookmark",
        lambda _karakeep_id: html_bookmark,
    )
    monkeypatch.setattr(
        "aizk.conversion.workers.orchestrator.validate_bookmark_content",
        lambda _bookmark: None,
    )
    # Bypass real conversion to keep the test focused on the flow mechanics.
    monkeypatch.setattr(
        "aizk.conversion.workers.orchestrator._prepare_conversion_input",
        lambda **_kwargs: ConversionInput(
            pipeline="html",
            content_bytes=b"<html><body>test</body></html>",
            fetched_at=dt.datetime.now(dt.timezone.utc),
        ),
    )
    monkeypatch.setattr(
        "aizk.conversion.workers.orchestrator.convert_html",
        lambda *_args, **_kwargs: ("# Test", []),
    )
    # Route S3Client behavior through our stubbed implementation.
    monkeypatch.setattr("aizk.conversion.workers.uploader.S3Client.__init__", _init_s3_client)
    monkeypatch.setattr("aizk.conversion.workers.uploader.S3Client.upload_file", _upload_file)

    with TestClient(app) as client:
        response = client.post(
            "/v1/jobs",
            json={
                "karakeep_id": "bm_test_001",
            },
        )
        assert response.status_code == 201
        job = response.json()
        job_id = job["id"]

        config = ConversionConfig(_env_file=None)
        orchestrator.process_job_supervised(job_id, config)

        job_response = client.get(f"/v1/jobs/{job_id}")
        assert job_response.status_code == 200
        assert job_response.json()["status"] == "SUCCEEDED"


def test_conversion_flow_cancelled_job_skips_upload(monkeypatch, html_bookmark):
    """Stop processing when a running job is cancelled."""
    monkeypatch.setattr(orchestrator.mp, "get_context", lambda _ctx: _InlineContext())
    app = create_app()
    if not any(getattr(route, "path", None) == "/v1/jobs" for route in app.router.routes):
        raise AssertionError("Jobs routes not registered in FastAPI app yet.")

    monkeypatch.setattr(
        "aizk.conversion.workers.orchestrator.fetch_karakeep_bookmark",
        lambda _karakeep_id: html_bookmark,
    )
    monkeypatch.setattr(
        "aizk.conversion.workers.orchestrator.validate_bookmark_content",
        lambda _bookmark: None,
    )
    monkeypatch.setattr(
        "aizk.conversion.workers.orchestrator._prepare_conversion_input",
        lambda **_kwargs: ConversionInput(
            pipeline="html",
            content_bytes=b"<html><body>cancel</body></html>",
            fetched_at=dt.datetime.now(dt.timezone.utc),
        ),
    )

    def _run_conversion(**kwargs):
        engine = get_engine(ConversionConfig(_env_file=None).database_url)
        with Session(engine) as session:
            job_record = session.get(ConversionJob, kwargs["job"].id)
            job_record.status = ConversionJobStatus.CANCELLED
            session.add(job_record)
            session.commit()

    def _upload_converted(_job_id, _workspace, _config):
        raise AssertionError("Upload should not run for cancelled jobs")

    monkeypatch.setattr("aizk.conversion.workers.orchestrator._run_conversion", _run_conversion)
    monkeypatch.setattr("aizk.conversion.workers.orchestrator._upload_converted", _upload_converted)

    with TestClient(app) as client:
        response = client.post(
            "/v1/jobs",
            json={
                "karakeep_id": "bm_cancel_001",
            },
        )
        assert response.status_code == 201
        job = response.json()
        job_id = job["id"]

        config = ConversionConfig(_env_file=None)
        orchestrator.process_job_supervised(job_id, config)

    engine = get_engine(ConversionConfig(_env_file=None).database_url)
    with Session(engine) as session:
        job_record = session.get(ConversionJob, job_id)
        assert job_record.status == ConversionJobStatus.CANCELLED

        output = session.exec(select(ConversionOutput).where(ConversionOutput.job_id == job_id)).first()
        assert output is None


def test_submit_job_idempotency_key_disables_picture_description_without_api_key(monkeypatch, db_session) -> None:
    """Idempotency must reflect actual picture-description runtime enablement."""
    monkeypatch.setenv("DOCLING_PICTURE_DESCRIPTION_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("DOCLING_PICTURE_DESCRIPTION_API_KEY", "")

    app = create_app()
    config = ConversionConfig(_env_file=None)

    with TestClient(app) as client:
        response = client.post(
            "/v1/jobs",
            json={
                "karakeep_id": "bm_missing_picture_api_key",
            },
        )

    assert response.status_code == 201
    payload = response.json()

    expected_key = compute_idempotency_key(
        UUID(payload["aizk_uuid"]),
        1,
        config,
        picture_description_enabled=False,
    )

    assert payload["idempotency_key"] == expected_key
