"""Integration test: whitespace normalization produces stable output.md across reruns.

Verifies that two conversions of the same bookmark content — where Docling emits
different whitespace artifacts on each run — produce byte-identical output.md files
and identical content hashes. This is the end-to-end stability guarantee promised
by the whitespace-normalization change.
"""

from __future__ import annotations

import hashlib
import json
import queue as queue_module

import boto3
from botocore.stub import ANY, Stubber
from sqlmodel import Session, select

from fastapi.testclient import TestClient

from aizk.conversion.api.main import create_app
from aizk.conversion.datamodel.output import ConversionOutput
from aizk.conversion.db import get_engine
from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.workers import orchestrator


class _InlineProcess:
    """Execute target immediately in same process (no subprocess)."""

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
    """Provide Queue and inline Process for testing."""

    def Queue(self):  # noqa: N802
        return queue_module.Queue()

    def Process(self, target, args, daemon: bool):  # noqa: N802
        return _InlineProcess(target, args)


# Two markdown outputs for the same content with different whitespace artifacts.
# Docling may emit either form across separate runs on the same source material.
# normalize_whitespace() must collapse both to the same result.
_MARKDOWN_WITH_ARTIFACTS = (
    "# Research Summary\n\nThis  is  the  body  of  the  document.\n\n\nMore  text  follows  here.\n"
)
_MARKDOWN_CLEAN = "# Research Summary\n\nThis is the body of the document.\n\nMore text follows here.\n"


def _make_subprocess_stub(markdowns_iter):
    """Return a _process_job_subprocess stub that writes pre-built markdown to workspace."""

    def _stub(job_id: int, workspace_path: str, source_ref_json: str, status_queue) -> None:
        from pathlib import Path

        from aizk.conversion.utilities.hashing import compute_markdown_hash
        from aizk.conversion.utilities.paths import OUTPUT_MARKDOWN_FILENAME, metadata_path
        from aizk.conversion.utilities.whitespace import normalize_whitespace

        workspace = Path(workspace_path)
        raw_markdown = next(markdowns_iter)
        normalized = normalize_whitespace(raw_markdown)

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
            "terminal_ref": {"kind": "karakeep_bookmark", "bookmark_id": "test"},
            "content_type": "html",
        }
        metadata_path(workspace).write_text(json.dumps(metadata))

        if status_queue:
            status_queue.put_nowait({"event": "phase", "message": "converting"})
            status_queue.put_nowait({"event": "completed", "message": "done"})

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


def test_whitespace_normalization_produces_stable_output(monkeypatch) -> None:
    """Two conversions with different whitespace artifacts produce identical output.md and hash."""
    monkeypatch.setattr(orchestrator.mp, "get_context", lambda _ctx: _InlineContext())
    app = create_app()

    markdowns = iter([_MARKDOWN_WITH_ARTIFACTS, _MARKDOWN_CLEAN])
    monkeypatch.setattr(orchestrator, "_process_job_subprocess", _make_subprocess_stub(markdowns))

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

    monkeypatch.setattr("aizk.conversion.workers.uploader.S3Client.__init__", _init_s3_client)
    monkeypatch.setattr("aizk.conversion.workers.uploader.S3Client.upload_file", _upload_file)

    runtime = _make_fake_runtime()

    with TestClient(app) as client:
        config = ConversionConfig(_env_file=None)

        resp1 = client.post(
            "/v1/jobs", json={"source_ref": {"kind": "karakeep_bookmark", "bookmark_id": "bm_ws_stable_001"}}
        )
        assert resp1.status_code == 201
        orchestrator.process_job_supervised(resp1.json()["id"], config, runtime)

        resp2 = client.post(
            "/v1/jobs", json={"source_ref": {"kind": "karakeep_bookmark", "bookmark_id": "bm_ws_stable_002"}}
        )
        assert resp2.status_code == 201
        orchestrator.process_job_supervised(resp2.json()["id"], config, runtime)

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
