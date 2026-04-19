"""Integration test for end-to-end conversion flow."""

from __future__ import annotations

import hashlib
import json
import queue as queue_module

import boto3
from botocore.stub import ANY, Stubber
from sqlmodel import Session, select

from fastapi.testclient import TestClient

from aizk.conversion.api.main import create_app
from aizk.conversion.core.source_ref import KarakeepBookmarkRef, compute_source_ref_hash
from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.datamodel.output import ConversionOutput
from aizk.conversion.db import get_engine
from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.utilities.hashing import build_output_config_snapshot, compute_idempotency_key
from aizk.conversion.workers import orchestrator


class _InlineProcess:
    """Process that executes target immediately in the same process for testing."""

    pid = None

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


def _make_subprocess_stub(markdown: str = "# Test"):
    """Return a _process_job_subprocess stub that writes pre-built markdown to workspace."""

    def _stub(job_id: int, workspace_path: str, source_ref_json: str, status_queue) -> None:
        from pathlib import Path

        from aizk.conversion.utilities.hashing import compute_markdown_hash
        from aizk.conversion.utilities.paths import OUTPUT_MARKDOWN_FILENAME, metadata_path
        from aizk.conversion.utilities.whitespace import normalize_whitespace

        workspace = Path(workspace_path)
        normalized = normalize_whitespace(markdown)
        markdown_file = workspace / OUTPUT_MARKDOWN_FILENAME
        markdown_file.write_text(normalized)

        metadata = {
            "pipeline_name": "html",
            "fetched_at": "2024-01-01T00:00:00+00:00",
            "markdown_filename": OUTPUT_MARKDOWN_FILENAME,
            "figure_files": [],
            "markdown_hash_xx64": compute_markdown_hash(normalized),
            "docling_version": "test",
            "config_snapshot": {"converter_name": "docling"},
            "terminal_ref": {"kind": "karakeep_bookmark", "bookmark_id": "bm_test_001"},
            "content_type": "html",
        }
        metadata_path(workspace).write_text(json.dumps(metadata))

        if status_queue:
            status_queue.put_nowait({"event": "phase", "message": "converting"})
            status_queue.put_nowait({"event": "completed", "message": "done"})

    return _stub


def _make_cancelling_subprocess_stub():
    """Stub that cancels the job in DB mid-execution and reports cancelled."""

    def _stub(job_id: int, workspace_path: str, source_ref_json: str, status_queue) -> None:
        engine = get_engine(ConversionConfig(_env_file=None).database_url)
        with Session(engine) as session:
            job_record = session.get(ConversionJob, job_id)
            if job_record:
                job_record.status = ConversionJobStatus.CANCELLED
                session.add(job_record)
                session.commit()
        if status_queue:
            status_queue.put_nowait({"event": "cancelled", "message": "cancelled"})

    return _stub


def _make_fake_runtime():
    from unittest.mock import MagicMock

    from aizk.conversion.wiring.worker import WorkerRuntime

    fake_caps = MagicMock()
    fake_caps.converter_requires_gpu.return_value = False
    return WorkerRuntime(
        orchestrator=MagicMock(),
        resource_guard=MagicMock(__enter__=MagicMock(return_value=None), __exit__=MagicMock(return_value=False)),
        capabilities=fake_caps,
    )


def test_conversion_flow_end_to_end(monkeypatch):
    """Exercise API submit + worker processing with stubbed external services."""
    monkeypatch.setattr(orchestrator.mp, "get_context", lambda _ctx: _InlineContext())
    monkeypatch.setattr(orchestrator, "_process_job_subprocess", _make_subprocess_stub())
    app = create_app()

    s3_client = boto3.client("s3", region_name="us-east-1")
    stubber = Stubber(s3_client)
    stubber.activate()

    def _init_s3_client(self, config):
        self.config = config
        self.client = s3_client
        self.bucket = config.s3_bucket_name

    def _upload_file(self, local_path, s3_key: str) -> str:
        body = local_path.read_bytes()
        stubber.add_response("put_object", {}, {"Bucket": self.bucket, "Key": s3_key, "Body": ANY})
        self.client.put_object(Bucket=self.bucket, Key=s3_key, Body=body)
        md5_hash = hashlib.md5(body).hexdigest()  # noqa: S324
        stubber.add_response(
            "head_object",
            {"ETag": f'"{md5_hash}"', "ContentLength": len(body)},
            {"Bucket": self.bucket, "Key": s3_key},
        )
        self.client.head_object(Bucket=self.bucket, Key=s3_key)
        return f"s3://{self.bucket}/{s3_key}"

    monkeypatch.setattr("aizk.conversion.workers.uploader.S3Client.__init__", _init_s3_client)
    monkeypatch.setattr("aizk.conversion.workers.uploader.S3Client.upload_file", _upload_file)

    with TestClient(app) as client:
        response = client.post(
            "/v1/jobs",
            json={"source_ref": {"kind": "karakeep_bookmark", "bookmark_id": "bm_test_001"}},
        )
        assert response.status_code == 201
        job_id = response.json()["id"]

        config = ConversionConfig(_env_file=None)
        orchestrator.process_job_supervised(job_id, config, _make_fake_runtime())

        job_response = client.get(f"/v1/jobs/{job_id}")
        assert job_response.status_code == 200
        assert job_response.json()["status"] == "SUCCEEDED"


def test_conversion_flow_cancelled_job_skips_upload(monkeypatch):
    """Stop processing when a running job is cancelled mid-execution."""
    monkeypatch.setattr(orchestrator.mp, "get_context", lambda _ctx: _InlineContext())
    monkeypatch.setattr(orchestrator, "_process_job_subprocess", _make_cancelling_subprocess_stub())

    upload_called = []
    monkeypatch.setattr(
        "aizk.conversion.workers.orchestrator._upload_converted",
        lambda _job_id, _workspace, _config: upload_called.append(True),
    )

    app = create_app()
    with TestClient(app) as client:
        response = client.post(
            "/v1/jobs",
            json={"source_ref": {"kind": "karakeep_bookmark", "bookmark_id": "bm_cancel_001"}},
        )
        assert response.status_code == 201
        job_id = response.json()["id"]

        config = ConversionConfig(_env_file=None)
        orchestrator.process_job_supervised(job_id, config, _make_fake_runtime())

    assert not upload_called, "Upload should not run for cancelled jobs"

    engine = get_engine(ConversionConfig(_env_file=None).database_url)
    with Session(engine) as session:
        job_record = session.get(ConversionJob, job_id)
        assert job_record.status == ConversionJobStatus.CANCELLED
        output = session.exec(select(ConversionOutput).where(ConversionOutput.job_id == job_id)).first()
        assert output is None


def test_submit_job_idempotency_key_disables_picture_description_without_api_key(monkeypatch) -> None:
    """Idempotency must reflect actual picture-description runtime enablement."""
    monkeypatch.setenv("DOCLING_PICTURE_DESCRIPTION_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("DOCLING_PICTURE_DESCRIPTION_API_KEY", "")

    app = create_app()
    config = ConversionConfig(_env_file=None)

    bookmark_id = "bm_missing_picture_api_key"
    with TestClient(app) as client:
        response = client.post(
            "/v1/jobs",
            json={"source_ref": {"kind": "karakeep_bookmark", "bookmark_id": bookmark_id}},
        )

    assert response.status_code == 201
    payload = response.json()

    source_ref = KarakeepBookmarkRef(kind="karakeep_bookmark", bookmark_id=bookmark_id)
    source_ref_hash = compute_source_ref_hash(source_ref)
    config_snap = build_output_config_snapshot(config, picture_description_enabled=False)
    expected_key = compute_idempotency_key(source_ref_hash, "docling", config_snap)

    assert payload["idempotency_key"] == expected_key
