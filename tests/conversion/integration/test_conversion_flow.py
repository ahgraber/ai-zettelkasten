"""Integration test for end-to-end conversion flow."""

from __future__ import annotations

import hashlib
import json
import queue as queue_module
from uuid import uuid4

import boto3
from botocore.stub import ANY, Stubber
from sqlmodel import Session, select

from fastapi.testclient import TestClient

from aizk.conversion.api.main import create_app
from aizk.conversion.core.source_ref import ArxivRef, KarakeepBookmarkRef, compute_source_ref_hash
from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.datamodel.output import ConversionOutput
from aizk.conversion.datamodel.source import Source
from aizk.conversion.db import get_engine
from aizk.conversion.utilities.config import ConversionConfig, DoclingConverterConfig
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


def _install_memory_s3(monkeypatch) -> dict[str, bytes]:
    storage: dict[str, bytes] = {}

    def _init_s3_client(self, config):
        self.config = config
        self.bucket = config.s3_bucket_name
        self.client = None

    def _upload_file(self, local_path, s3_key: str) -> str:
        storage[s3_key] = local_path.read_bytes()
        return f"s3://{self.bucket}/{s3_key}"

    def _get_object_bytes(self, s3_key: str) -> bytes:
        return storage[s3_key]

    monkeypatch.setattr("aizk.conversion.storage.s3_client.S3Client.__init__", _init_s3_client)
    monkeypatch.setattr("aizk.conversion.storage.s3_client.S3Client.upload_file", _upload_file)
    monkeypatch.setattr("aizk.conversion.storage.s3_client.S3Client.get_object_bytes", _get_object_bytes)
    return storage


def _fake_convert_pdf(content: bytes, temp_dir, config):
    return f"# {content.decode('ascii')}\n", []


def _create_job_for_ref(db_session, ref, *, idempotency_key: str) -> int:
    source = Source(
        aizk_uuid=uuid4(),
        karakeep_id=ref.bookmark_id if isinstance(ref, KarakeepBookmarkRef) else None,
        source_ref=ref.model_dump_json(),
        source_ref_hash=compute_source_ref_hash(ref),
        title="Integration Source",
    )
    db_session.add(source)
    db_session.commit()
    db_session.refresh(source)

    job = ConversionJob(
        aizk_uuid=source.aizk_uuid,
        title="Integration Job",
        payload_version=1,
        status=ConversionJobStatus.QUEUED,
        attempts=0,
        idempotency_key=idempotency_key,
        source_ref=ref.model_dump_json(),
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    return job.id


def _process_and_get_markdown(db_session, monkeypatch, *, ref, idempotency_key: str) -> str:
    monkeypatch.setattr(orchestrator.mp, "get_context", lambda _ctx: _InlineContext())
    monkeypatch.setattr("aizk.conversion.adapters.converters.docling.convert_pdf", _fake_convert_pdf)
    storage = _install_memory_s3(monkeypatch)

    job_id = _create_job_for_ref(db_session, ref, idempotency_key=idempotency_key)
    config = ConversionConfig(_env_file=None)
    orchestrator.process_job_supervised(job_id, config, _make_fake_runtime())

    engine = get_engine(config.database_url)
    with Session(engine) as session:
        output = session.exec(select(ConversionOutput).where(ConversionOutput.job_id == job_id)).one()
        job_record = session.get(ConversionJob, job_id)
        assert job_record.status == ConversionJobStatus.SUCCEEDED
        return storage[output.markdown_key].decode("utf-8")


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
    monkeypatch.setenv("AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_API_KEY", "")

    app = create_app()
    docling_cfg = DoclingConverterConfig(_env_file=None)

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
    config_snap = build_output_config_snapshot(docling_cfg, picture_description_enabled=False)
    expected_key = compute_idempotency_key(source_ref_hash, "docling", config_snap)

    assert payload["idempotency_key"] == expected_key


def test_conversion_flow_proceeds_when_source_enrichment_fails(monkeypatch, db_session) -> None:
    monkeypatch.setattr(orchestrator.mp, "get_context", lambda _ctx: _InlineContext())
    monkeypatch.setattr("aizk.conversion.adapters.converters.docling.convert_pdf", _fake_convert_pdf)
    storage = _install_memory_s3(monkeypatch)

    async def _fake_fetch_url(url: str, timeout: int) -> bytes:
        assert url == "https://arxiv.org/pdf/2301.12345"
        return b"direct-pdf-url"

    def _boom(*args, **kwargs):
        raise RuntimeError("db write failed")

    monkeypatch.setattr("aizk.conversion.adapters.fetchers.arxiv._fetch_url", _fake_fetch_url)
    monkeypatch.setattr(orchestrator, "_enrich_source_metadata", _boom)

    ref = ArxivRef(arxiv_id="2301.12345", arxiv_pdf_url="https://arxiv.org/pdf/2301.12345")
    job_id = _create_job_for_ref(db_session, ref, idempotency_key="4" * 64)
    config = ConversionConfig(_env_file=None)

    orchestrator.process_job_supervised(job_id, config, _make_fake_runtime())

    engine = get_engine(config.database_url)
    with Session(engine) as session:
        output = session.exec(select(ConversionOutput).where(ConversionOutput.job_id == job_id)).one()
        job_record = session.get(ConversionJob, job_id)
        assert job_record.status == ConversionJobStatus.SUCCEEDED
        assert storage[output.markdown_key].decode("utf-8") == "# direct-pdf-url\n"


def test_arxiv_precedence_uses_karakeep_pdf_asset_end_to_end(monkeypatch, db_session) -> None:
    from karakeep_client.models import Bookmark

    bookmark = Bookmark.model_validate(
        {
            "id": "bm_arxiv_asset",
            "createdAt": "2025-01-01T00:00:00.000Z",
            "modifiedAt": "2025-01-01T00:00:00.000Z",
            "title": None,
            "archived": False,
            "favourited": False,
            "taggingStatus": "success",
            "summarizationStatus": "success",
            "note": None,
            "summary": None,
            "tags": [],
            "content": {
                "type": "asset",
                "assetType": "pdf",
                "assetId": "asset-step1",
                "fileName": "paper.pdf",
                "sourceUrl": "https://arxiv.org/abs/2301.12345",
                "size": 1000.0,
                "content": None,
            },
            "assets": [{"id": "asset-step1", "assetType": "bookmarkAsset"}],
        }
    )

    async def _fake_karakeep_asset(asset_id: str) -> bytes:
        assert asset_id == "asset-step1"
        return b"karakeep-asset"

    async def _should_not_fetch_url(url: str, timeout: int) -> bytes:
        raise AssertionError("_fetch_url must not be used when KaraKeep asset URL is present")

    async def _should_not_fetch_arxiv_pdf(arxiv_id: str, config) -> bytes:
        raise AssertionError("fetch_arxiv_pdf must not be used when KaraKeep asset URL is present")

    monkeypatch.setattr("aizk.conversion.adapters.fetchers.karakeep.fetch_karakeep_bookmark", lambda _id: bookmark)
    monkeypatch.setattr("aizk.conversion.adapters.fetchers.karakeep.detect_source_type", lambda _url: "arxiv")
    monkeypatch.setattr("aizk.conversion.adapters.fetchers.arxiv.fetch_karakeep_asset", _fake_karakeep_asset)
    monkeypatch.setattr("aizk.conversion.adapters.fetchers.arxiv._fetch_url", _should_not_fetch_url)
    monkeypatch.setattr("aizk.conversion.adapters.fetchers.arxiv.fetch_arxiv_pdf", _should_not_fetch_arxiv_pdf)

    markdown = _process_and_get_markdown(
        db_session,
        monkeypatch,
        ref=KarakeepBookmarkRef(bookmark_id="bm_arxiv_asset"),
        idempotency_key="1" * 64,
    )

    assert markdown == "# karakeep-asset\n"


def test_arxiv_precedence_uses_direct_arxiv_pdf_url_end_to_end(monkeypatch, db_session) -> None:
    async def _fake_fetch_url(url: str, timeout: int) -> bytes:
        assert url == "https://arxiv.org/pdf/2301.12345"
        return b"direct-pdf-url"

    async def _should_not_fetch_karakeep_asset(asset_id: str) -> bytes:
        raise AssertionError("fetch_karakeep_asset must not be used for non-KaraKeep arxiv_pdf_url")

    async def _should_not_fetch_arxiv_pdf(arxiv_id: str, config) -> bytes:
        raise AssertionError("fetch_arxiv_pdf must not be used when arxiv_pdf_url is present")

    monkeypatch.setattr("aizk.conversion.adapters.fetchers.arxiv._fetch_url", _fake_fetch_url)
    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.arxiv.fetch_karakeep_asset", _should_not_fetch_karakeep_asset
    )
    monkeypatch.setattr("aizk.conversion.adapters.fetchers.arxiv.fetch_arxiv_pdf", _should_not_fetch_arxiv_pdf)

    markdown = _process_and_get_markdown(
        db_session,
        monkeypatch,
        ref=ArxivRef(arxiv_id="2301.12345", arxiv_pdf_url="https://arxiv.org/pdf/2301.12345"),
        idempotency_key="2" * 64,
    )

    assert markdown == "# direct-pdf-url\n"


def test_arxiv_precedence_uses_abstract_page_resolution_end_to_end(monkeypatch, db_session) -> None:
    async def _fake_fetch_arxiv_pdf(arxiv_id: str, config) -> bytes:
        assert arxiv_id == "2301.12345"
        return b"abstract-page"

    async def _should_not_fetch_karakeep_asset(asset_id: str) -> bytes:
        raise AssertionError("fetch_karakeep_asset must not be used without KaraKeep asset URL")

    async def _should_not_fetch_url(url: str, timeout: int) -> bytes:
        raise AssertionError("_fetch_url must not be used without arxiv_pdf_url")

    monkeypatch.setattr("aizk.conversion.adapters.fetchers.arxiv.fetch_arxiv_pdf", _fake_fetch_arxiv_pdf)
    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.arxiv.fetch_karakeep_asset", _should_not_fetch_karakeep_asset
    )
    monkeypatch.setattr("aizk.conversion.adapters.fetchers.arxiv._fetch_url", _should_not_fetch_url)

    markdown = _process_and_get_markdown(
        db_session,
        monkeypatch,
        ref=ArxivRef(arxiv_id="2301.12345", arxiv_pdf_url=None),
        idempotency_key="3" * 64,
    )

    assert markdown == "# abstract-page\n"
