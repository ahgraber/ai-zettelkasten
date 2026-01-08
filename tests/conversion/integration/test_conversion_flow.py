"""Integration test for end-to-end conversion flow."""

from __future__ import annotations

import datetime as dt
import hashlib

import boto3
from botocore.stub import ANY, Stubber
from sqlalchemy.orm import Mapped

from fastapi.testclient import TestClient

from aizk.conversion.api.main import create_app
from aizk.conversion.datamodel.bookmark import Bookmark
from aizk.conversion.datamodel.job import ConversionJob
from aizk.conversion.datamodel.output import ConversionOutput
from aizk.conversion.workers.worker import ConversionInput


def test_conversion_flow_end_to_end(monkeypatch, html_bookmark):
    """Exercise API submit + worker processing with stubbed external services."""
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
        "aizk.conversion.workers.worker.fetch_karakeep_bookmark",
        lambda _karakeep_id: html_bookmark,
    )
    monkeypatch.setattr(
        "aizk.conversion.workers.worker.validate_bookmark_content",
        lambda _bookmark: None,
    )
    # Bypass real conversion to keep the test focused on the flow mechanics.
    monkeypatch.setattr(
        "aizk.conversion.workers.worker._prepare_conversion_input",
        lambda **_kwargs: ConversionInput(
            pipeline="html",
            content_bytes=b"<html><body>test</body></html>",
            fetched_at=dt.datetime.now(dt.timezone.utc),
        ),
    )
    monkeypatch.setattr(
        "aizk.conversion.workers.worker.convert_html",
        lambda *_args, **_kwargs: ("# Test", []),
    )
    # Route S3Client behavior through our stubbed implementation.
    monkeypatch.setattr("aizk.conversion.workers.worker.S3Client.__init__", _init_s3_client)
    monkeypatch.setattr("aizk.conversion.workers.worker.S3Client.upload_file", _upload_file)

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

        from aizk.conversion.workers.worker import process_job

        process_job(job_id)

        job_response = client.get(f"/v1/jobs/{job_id}")
        assert job_response.status_code == 200
        assert job_response.json()["status"] == "SUCCEEDED"
