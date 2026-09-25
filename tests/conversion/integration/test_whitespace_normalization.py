"""Integration test: whitespace normalization produces stable output.md across reruns.

Verifies that two conversions of the same bookmark content — where Docling emits
different whitespace artifacts on each run — produce byte-identical output.md files
and identical content hashes. This is the end-to-end stability guarantee promised
by the whitespace-normalization change.
"""

from __future__ import annotations

import json

from sqlmodel import Session, select

from fastapi.testclient import TestClient

from aizk.conversion.api.main import create_app
from aizk.conversion.datamodel.job import ConversionJob
from aizk.conversion.datamodel.output import ConversionOutput
from aizk.conversion.handler import ConversionStageHandler
from aizk.conversion.settings import ConversionConfig
from aizk.conversion.wiring import subprocess as wiring_subprocess
from aizk.db.config import DatabaseConfig
from aizk.db.engine import get_engine
from aizk.pipeline.lifecycle import WorkUnitStatus
from tests.fakes.process import InlineContext
from tests.fakes.runtime import fake_worker_runtime
from tests.fakes.storage import install_memory_s3


def _run_conversion(job_id: int, config: ConversionConfig, runtime) -> None:
    """Drive one conversion attempt through the runner adapter (RUNNING-seed).

    Marks the QUEUED job RUNNING (as ``claim_next`` would) and runs the adapter's
    :meth:`ConversionStageHandler.execute`, which performs spawn + supervise +
    enrich + upload phase + upload→SUCCEEDED through the same conversion helpers.
    """
    engine = get_engine(DatabaseConfig().database_url)
    with Session(engine) as session:
        job = session.get(ConversionJob, job_id)
        if job is not None and job.status == WorkUnitStatus.QUEUED:
            job.status = WorkUnitStatus.RUNNING
            job.attempts += 1
            session.add(job)
            session.commit()
    ConversionStageHandler(config, runtime=runtime).execute(job_id)


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

        from aizk.conversion.core.hashing import compute_markdown_hash
        from aizk.conversion.processing.whitespace import normalize_whitespace
        from aizk.conversion.storage.paths import OUTPUT_MARKDOWN_FILENAME, metadata_path

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
            "source_meta": {},
            "document_title": None,
            "source_title": None,
        }
        metadata_path(workspace).write_text(json.dumps(metadata))

        if status_queue:
            status_queue.put_nowait({"event": "phase", "message": "converting"})
            status_queue.put_nowait({"event": "completed", "message": "done"})

    return _stub


def test_whitespace_normalization_produces_stable_output(monkeypatch) -> None:
    """Two conversions with different whitespace artifacts produce identical output.md and hash."""
    monkeypatch.setattr(wiring_subprocess.mp, "get_context", lambda _ctx: InlineContext())
    app = create_app()

    markdowns = iter([_MARKDOWN_WITH_ARTIFACTS, _MARKDOWN_CLEAN])
    monkeypatch.setattr(wiring_subprocess, "_process_job_subprocess", _make_subprocess_stub(markdowns))

    storage = install_memory_s3(monkeypatch)

    runtime = fake_worker_runtime()

    with TestClient(app) as client:
        config = ConversionConfig(_env_file=None)

        resp1 = client.post(
            "/v1/jobs", json={"source_ref": {"kind": "karakeep_bookmark", "bookmark_id": "bm_ws_stable_001"}}
        )
        assert resp1.status_code == 201
        _run_conversion(resp1.json()["id"], config, runtime)

        resp2 = client.post(
            "/v1/jobs", json={"source_ref": {"kind": "karakeep_bookmark", "bookmark_id": "bm_ws_stable_002"}}
        )
        assert resp2.status_code == 201
        _run_conversion(resp2.json()["id"], config, runtime)

    captured_markdown_bodies = [body for s3_key, body in storage.items() if s3_key.endswith("/output.md")]
    assert len(captured_markdown_bodies) == 2, "output.md should be uploaded for both jobs"
    assert captured_markdown_bodies[0] == captured_markdown_bodies[1], (
        "output.md is not byte-identical across two conversions with different whitespace artifacts"
    )

    engine = get_engine(DatabaseConfig().database_url)
    with Session(engine) as session:
        outputs = session.exec(select(ConversionOutput).order_by(ConversionOutput.id)).all()
    assert len(outputs) == 2
    assert outputs[0].markdown_hash_xx64 == outputs[1].markdown_hash_xx64, (
        "Content hashes differ between two conversions of the same content with different whitespace"
    )
