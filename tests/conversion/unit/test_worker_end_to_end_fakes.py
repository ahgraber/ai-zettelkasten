"""End-to-end fakes-based test: KarakeepBookmarkRef → fetch → convert → upload.

Covers the gap the review noted: the full worker pipeline was only
exercised under ``tests/conversion/integration/test_conversion_flow.py``
which requires docling in the test env. This unit-level e2e uses a fake
orchestrator + fake converter so it runs in the hermetic unit env.

The tests exercise the parent-process path around ``_convert_job_artifacts``
without spawning a real subprocess: we invoke the body with injected
fakes for the WorkerRuntime and its orchestrator, verifying the
fetch→convert→persist pipeline wires up correctly.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from aizk.conversion.core.source_ref import (
    ArxivRef,
    KarakeepBookmarkRef,
    UrlRef,
    compute_source_ref_hash,
)
from aizk.conversion.core.types import ContentType, ConversionArtifacts, ConversionInput


@pytest.fixture()
def fake_runtime():
    """Build a WorkerRuntime-shaped object with fakes for orchestrator + registry."""
    runtime = MagicMock()
    runtime.converter_name = "fake"
    runtime.converter_requires_gpu.return_value = False

    fake_artifacts = ConversionArtifacts(markdown="# hello", figures=[], metadata={})
    fake_converter = MagicMock()
    fake_converter.convert.return_value = fake_artifacts

    def _fake_resolve(content_type, name):
        assert name == "fake"
        return fake_converter

    runtime.converter_registry.resolve.side_effect = _fake_resolve

    fake_orch = MagicMock()
    fake_orch.fetch.return_value = ConversionInput(
        content=b"<html>body</html>",
        content_type=ContentType.HTML,
        metadata={"source_url": "https://example.com"},
    )
    runtime.orchestrator = fake_orch

    return runtime


def test_convert_job_artifacts_end_to_end_with_fakes(
    tmp_path, fake_runtime, monkeypatch, db_session
):
    """Drive ``_convert_job_artifacts`` through a full fetch → convert → persist cycle."""
    from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
    from aizk.conversion.datamodel.source import Source
    from aizk.conversion.workers import orchestrator

    # Seed a Source + RUNNING job so _raise_if_cancelled and id lookups succeed.
    ref = ArxivRef(arxiv_id="2401.00001")
    source = Source(
        source_ref=ref.model_dump(),
        source_ref_hash=compute_source_ref_hash(ref),
    )
    db_session.add(source)
    db_session.commit()
    db_session.refresh(source)
    job = ConversionJob(
        aizk_uuid=source.aizk_uuid,
        source_ref=ref.model_dump(),
        title="e2e",
        status=ConversionJobStatus.RUNNING,
        idempotency_key="e" * 64,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    # Write the refined ref where the subprocess would expect it.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    source_ref_path = workspace / "source_ref.json"
    source_ref_path.write_text(ref.model_dump_json())

    # Patch the heavy seams: build_worker_runtime returns our fake; engine
    # uses the test db; _persist_artifacts is mocked so we don't exercise
    # its filesystem layout here (it has its own dedicated test).
    monkeypatch.setattr(
        orchestrator, "build_worker_runtime", lambda _cfg: fake_runtime
    )
    monkeypatch.setattr(
        orchestrator, "get_engine", lambda _url=None: db_session.get_bind()
    )
    persisted: dict = {}

    def _fake_persist(**kwargs):
        persisted.update(kwargs)

    monkeypatch.setattr(orchestrator, "_persist_artifacts", _fake_persist)

    # Run the subprocess body inline.
    orchestrator._convert_job_artifacts(
        job_id=job.id,
        workspace=workspace,
        source_ref_path=source_ref_path,
        status_queue=None,
    )

    # Orchestrator.fetch was called with the refined ArxivRef, once.
    fake_runtime.orchestrator.fetch.assert_called_once()
    called_ref = fake_runtime.orchestrator.fetch.call_args.args[0]
    assert isinstance(called_ref, ArxivRef)
    assert called_ref.arxiv_id == "2401.00001"

    # Converter was resolved by (ContentType.HTML, "fake") and invoked.
    fake_runtime.converter_registry.resolve.assert_called_once()
    args = fake_runtime.converter_registry.resolve.call_args.args
    assert args[0] is ContentType.HTML
    assert args[1] == "fake"

    # Persist got the fake artifacts and the right content type.
    assert persisted["content_type"] is ContentType.HTML
    assert persisted["artifacts"].markdown == "# hello"
    assert persisted["converter_name"] == "fake"


def test_convert_job_artifacts_raises_on_empty_fetcher_output(
    tmp_path, fake_runtime, monkeypatch, db_session
):
    """An empty ConversionInput from the orchestrator becomes a permanent failure."""
    from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
    from aizk.conversion.datamodel.source import Source
    from aizk.conversion.utilities.bookmark_utils import BookmarkContentUnavailableError
    from aizk.conversion.workers import orchestrator

    # Empty content path.
    fake_runtime.orchestrator.fetch.return_value = ConversionInput(
        content=b"", content_type=ContentType.HTML
    )

    ref = UrlRef(url="https://example.com/empty")
    source = Source(
        source_ref=ref.model_dump(),
        source_ref_hash=compute_source_ref_hash(ref),
    )
    db_session.add(source)
    db_session.commit()
    db_session.refresh(source)
    job = ConversionJob(
        aizk_uuid=source.aizk_uuid,
        source_ref=ref.model_dump(),
        title="e2e-empty",
        status=ConversionJobStatus.RUNNING,
        idempotency_key="f" * 64,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    source_ref_path = workspace / "source_ref.json"
    source_ref_path.write_text(ref.model_dump_json())

    monkeypatch.setattr(orchestrator, "build_worker_runtime", lambda _cfg: fake_runtime)
    monkeypatch.setattr(orchestrator, "get_engine", lambda _url=None: db_session.get_bind())

    with pytest.raises(BookmarkContentUnavailableError):
        orchestrator._convert_job_artifacts(
            job_id=job.id,
            workspace=workspace,
            source_ref_path=source_ref_path,
            status_queue=None,
        )
