"""Integration test: whitespace normalization produces stable output.md across reruns.

Verifies that two conversions of the same bookmark content — where Docling emits
different whitespace artifacts on each run — produce byte-identical output.md files
and identical content hashes. This is the end-to-end stability guarantee promised
by the whitespace-normalization change.
"""

from __future__ import annotations

import datetime as dt
import hashlib

import boto3
from botocore.stub import ANY, Stubber
from sqlmodel import Session, select

from fastapi.testclient import TestClient

from aizk.conversion.api.main import create_app
from aizk.conversion.datamodel.output import ConversionOutput
from aizk.conversion.db import get_engine
from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.workers import worker
from aizk.conversion.workers.worker import ConversionInput


class _InlineProcess:
    """Execute target immediately in same process (no subprocess)."""

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
    """Provide Queue and inline Process for testing."""

    def Queue(self):  # noqa: N802
        return worker.queue_module.Queue()

    def Process(self, target, args, daemon: bool):  # noqa: N802
        return _InlineProcess(target, args)


# Two markdown outputs for the same content with different whitespace artifacts.
# Docling may emit either form across separate runs on the same source material.
# normalize_whitespace() must collapse both to the same result.
_MARKDOWN_WITH_ARTIFACTS = (
    "# Research Summary\n\nThis  is  the  body  of  the  document.\n\n\nMore  text  follows  here.\n"
)
_MARKDOWN_CLEAN = "# Research Summary\n\nThis is the body of the document.\n\nMore text follows here.\n"


def test_whitespace_normalization_produces_stable_output(monkeypatch, html_bookmark) -> None:
    """Two conversions with different whitespace artifacts produce identical output.md and hash."""
    monkeypatch.setattr(worker.mp, "get_context", lambda _ctx: _InlineContext())
    app = create_app()

    markdowns = iter([_MARKDOWN_WITH_ARTIFACTS, _MARKDOWN_CLEAN])

    s3_client = boto3.client("s3", region_name="us-east-1")
    stubber = Stubber(s3_client)
    stubber.activate()

    captured_markdown_bodies: list[bytes] = []

    def _init_s3_client(self, config) -> None:
        self.config = config
        self.client = s3_client
        self.bucket = config.s3_bucket_name

    def _upload_file(self, local_path, s3_key: str) -> str:
        body = local_path.read_bytes()
        if local_path.name == "output.md":
            captured_markdown_bodies.append(body)
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

    monkeypatch.setattr(
        "aizk.conversion.workers.worker.fetch_karakeep_bookmark",
        lambda _: html_bookmark,
    )
    monkeypatch.setattr(
        "aizk.conversion.workers.worker.validate_bookmark_content",
        lambda _: None,
    )
    monkeypatch.setattr(
        "aizk.conversion.workers.worker._prepare_conversion_input",
        lambda **_: ConversionInput(
            pipeline="html",
            content_bytes=b"<html><body>test</body></html>",
            fetched_at=dt.datetime.now(dt.timezone.utc),
        ),
    )
    monkeypatch.setattr(
        "aizk.conversion.workers.worker.convert_html",
        lambda *_, **__: (next(markdowns), []),
    )
    monkeypatch.setattr("aizk.conversion.workers.worker.S3Client.__init__", _init_s3_client)
    monkeypatch.setattr("aizk.conversion.workers.worker.S3Client.upload_file", _upload_file)

    with TestClient(app) as client:
        config = ConversionConfig(_env_file=None)

        resp1 = client.post("/v1/jobs", json={"karakeep_id": "bm_ws_stable_001"})
        assert resp1.status_code == 201
        worker.process_job_supervised(resp1.json()["id"], config)

        resp2 = client.post("/v1/jobs", json={"karakeep_id": "bm_ws_stable_002"})
        assert resp2.status_code == 201
        worker.process_job_supervised(resp2.json()["id"], config)

    assert len(captured_markdown_bodies) == 2, "output.md should be uploaded for both jobs"
    assert captured_markdown_bodies[0] == captured_markdown_bodies[1], (
        "output.md is not byte-identical across two conversions with different whitespace artifacts"
    )

    engine = get_engine(ConversionConfig(_env_file=None).database_url)
    with Session(engine) as session:
        outputs = session.exec(select(ConversionOutput).order_by(ConversionOutput.id)).all()
    assert len(outputs) == 2
    assert outputs[0].markdown_hash_xx64 == outputs[1].markdown_hash_xx64, (
        "Content hashes differ between two conversions of the same content with different whitespace"
    )
