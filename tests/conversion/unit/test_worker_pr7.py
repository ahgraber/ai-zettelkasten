"""Unit tests for PR 7 worker cutover: ResourceGuard, source_ref dispatch, enrichment.

These tests exercise the worker's orchestration logic without requiring
docling to be installed.  Docling-dependent end-to-end tests live in
tests/conversion/integration/test_conversion_flow.py and run only in the
full environment.
"""

from __future__ import annotations

import datetime as dt
import json
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlmodel import Session, select

from aizk.conversion.core.source_ref import (
    ArxivRef,
    InlineHtmlRef,
    KarakeepBookmarkRef,
    UrlRef,
    compute_source_ref_hash,
    parse_source_ref,
)
from aizk.conversion.core.types import ContentType, ConversionArtifacts
from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.datamodel.source import Source


# ---------------------------------------------------------------------------
# parse_source_ref helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload,expected_type",
    [
        ({"kind": "karakeep_bookmark", "bookmark_id": "bm1"}, KarakeepBookmarkRef),
        ({"kind": "arxiv", "arxiv_id": "2301.12345"}, ArxivRef),
        ({"kind": "url", "url": "https://example.com"}, UrlRef),
    ],
)
def test_parse_source_ref_dispatches_by_kind(payload, expected_type):
    ref = parse_source_ref(payload)
    assert isinstance(ref, expected_type)


def test_parse_source_ref_rejects_unknown_kind():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        parse_source_ref({"kind": "unknown_kind", "foo": "bar"})


# ---------------------------------------------------------------------------
# WorkerRuntime.converter_requires_gpu
# ---------------------------------------------------------------------------


class _FakeGpuConverter:
    supported_formats = frozenset({ContentType.PDF})
    requires_gpu = True

    def convert(self, conversion_input):
        return ConversionArtifacts(markdown="# gpu", figures=[])

    def config_snapshot(self):
        return {}


class _FakeCpuConverter:
    supported_formats = frozenset({ContentType.HTML})
    requires_gpu = False

    def convert(self, conversion_input):
        return ConversionArtifacts(markdown="# cpu", figures=[])

    def config_snapshot(self):
        return {}


def _make_runtime_with_converter(impl, name: str):
    from aizk.conversion.core.orchestrator import Orchestrator
    from aizk.conversion.core.registry import ConverterRegistry, FetcherRegistry
    from aizk.conversion.wiring.capabilities import DeploymentCapabilities
    from aizk.conversion.wiring.worker import WorkerRuntime, _SemaphoreGuard

    fetcher_registry = FetcherRegistry()
    converter_registry = ConverterRegistry()
    converter_registry.register(name, impl)
    capabilities = DeploymentCapabilities(
        accepted_kinds=frozenset(),
        content_type_map={},
        registered_content_types=converter_registry.registered_formats(),
        startup_probes=[],
    )
    return WorkerRuntime(
        orchestrator=Orchestrator(
            resolve_fetcher=fetcher_registry.resolve,
            resolve_converter=converter_registry.resolve,
        ),
        gpu_guard=_SemaphoreGuard(n=1),
        capabilities=capabilities,
        fetcher_registry=fetcher_registry,
        converter_registry=converter_registry,
        converter_name=name,
    )


def test_converter_requires_gpu_true_for_gpu_converter():
    runtime = _make_runtime_with_converter(_FakeGpuConverter(), "fake_gpu")
    assert runtime.converter_requires_gpu() is True


def test_converter_requires_gpu_false_for_cpu_converter():
    runtime = _make_runtime_with_converter(_FakeCpuConverter(), "fake_cpu")
    assert runtime.converter_requires_gpu() is False


def test_converter_requires_gpu_false_for_unregistered_name():
    runtime = _make_runtime_with_converter(_FakeGpuConverter(), "fake_gpu")
    assert runtime.converter_requires_gpu("nonexistent") is False


# ---------------------------------------------------------------------------
# ResourceGuard acquisition semantics (via _SemaphoreGuard)
# ---------------------------------------------------------------------------


def test_semaphore_guard_blocks_concurrent_acquire():
    """A second thread entering the guard's with block blocks until the first exits."""
    from aizk.conversion.wiring.worker import _SemaphoreGuard

    guard = _SemaphoreGuard(n=1)
    acquired_first = threading.Event()
    acquired_second = threading.Event()
    release_first = threading.Event()

    def first():
        with guard:
            acquired_first.set()
            release_first.wait(timeout=5.0)

    def second():
        with guard:
            acquired_second.set()

    t1 = threading.Thread(target=first)
    t2 = threading.Thread(target=second)
    t1.start()
    acquired_first.wait(timeout=5.0)

    t2.start()
    # Second thread must be blocked
    assert not acquired_second.wait(timeout=0.2)

    release_first.set()
    t1.join(timeout=5.0)
    # Now second thread should acquire
    assert acquired_second.wait(timeout=5.0)
    t2.join(timeout=5.0)


def test_semaphore_guard_non_gpu_does_not_block_gpu():
    """A converter with requires_gpu=False does not contend on the guard.

    Simulated by having one thread hold the guard and a second thread NOT
    entering the guard at all — it should proceed immediately regardless.
    """
    from aizk.conversion.wiring.worker import _SemaphoreGuard

    guard = _SemaphoreGuard(n=1)
    guard_acquired = threading.Event()
    release = threading.Event()
    bypassed_flag = threading.Event()

    def gpu_thread():
        with guard:
            guard_acquired.set()
            release.wait(timeout=5.0)

    def bypass_thread():
        # Simulates the requires_gpu == False path: no guard acquisition.
        bypassed_flag.set()

    t1 = threading.Thread(target=gpu_thread)
    t1.start()
    guard_acquired.wait(timeout=5.0)

    t2 = threading.Thread(target=bypass_thread)
    t2.start()
    # Bypass thread is NOT blocked by the held guard
    assert bypassed_flag.wait(timeout=0.5)
    t2.join(timeout=2.0)

    release.set()
    t1.join(timeout=5.0)


# ---------------------------------------------------------------------------
# _enrich_source_for_job: mutable metadata only
# ---------------------------------------------------------------------------


def test_enrich_source_for_arxiv_ref_does_not_rewrite_identity_columns(
    db_session, tmp_path, monkeypatch
):
    """Enriching an ArxivRef job populates mutable metadata; identity columns unchanged."""
    from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
    from aizk.conversion.workers.orchestrator import _enrich_source_for_job

    # Create Source with ArxivRef identity (as if API created it).
    ref = ArxivRef(arxiv_id="2301.99999")
    source_ref_payload = ref.model_dump()
    source = Source(
        karakeep_id=None,
        source_ref=source_ref_payload,
        source_ref_hash=compute_source_ref_hash(ref),
        url=None,
        title=None,
        source_type=None,
    )
    db_session.add(source)
    db_session.commit()
    db_session.refresh(source)

    identity_snapshot = {
        "aizk_uuid": source.aizk_uuid,
        "source_ref": dict(source.source_ref),
        "source_ref_hash": source.source_ref_hash,
        "karakeep_id": source.karakeep_id,
    }

    job = ConversionJob(
        aizk_uuid=source.aizk_uuid,
        source_ref=source_ref_payload,
        title="initial",
        status=ConversionJobStatus.NEW,
        idempotency_key="x" * 64,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    # Run the enrichment — it reads job.source_ref (ArxivRef) directly and
    # derives enrichment without hitting KaraKeep.
    from aizk.conversion.utilities.config import ConversionConfig

    engine = db_session.get_bind()
    _, returned_ref = _enrich_source_for_job(job.id, engine, ConversionConfig(_env_file=None))

    # Re-read the Source
    db_session.expire_all()
    updated = db_session.exec(
        select(Source).where(Source.aizk_uuid == source.aizk_uuid)
    ).one()

    # Identity columns unchanged
    assert updated.aizk_uuid == identity_snapshot["aizk_uuid"]
    assert updated.source_ref == identity_snapshot["source_ref"]
    assert updated.source_ref_hash == identity_snapshot["source_ref_hash"]
    assert updated.karakeep_id == identity_snapshot["karakeep_id"]

    # Mutable metadata populated
    assert updated.source_type == "arxiv"
    assert updated.content_type == "pdf"
    assert updated.url == "https://arxiv.org/abs/2301.99999"
    assert updated.normalized_url

    # Ref round-trip
    assert isinstance(returned_ref, ArxivRef)
    assert returned_ref.arxiv_id == "2301.99999"


def test_enrich_source_for_inline_html_ref(db_session):
    """InlineHtmlRef enrichment sets content_type='html'; url/title stay null."""
    from aizk.conversion.workers.orchestrator import _enrich_source_for_job

    body = b"<html><body><pre>hello</pre></body></html>"
    ref = InlineHtmlRef(body=body)
    # mode="json" base64-encodes bytes for JSON column storage.
    source_ref_payload = ref.model_dump(mode="json")
    source = Source(
        source_ref=source_ref_payload,
        source_ref_hash=compute_source_ref_hash(ref),
        url=None,
        title=None,
    )
    db_session.add(source)
    db_session.commit()
    db_session.refresh(source)

    job = ConversionJob(
        aizk_uuid=source.aizk_uuid,
        source_ref=source_ref_payload,
        title="inline",
        status=ConversionJobStatus.NEW,
        idempotency_key="i" * 64,
    )
    db_session.add(job)
    db_session.commit()

    from aizk.conversion.utilities.config import ConversionConfig

    engine = db_session.get_bind()
    _, returned_ref = _enrich_source_for_job(job.id, engine, ConversionConfig(_env_file=None))

    db_session.expire_all()
    updated = db_session.exec(
        select(Source).where(Source.aizk_uuid == source.aizk_uuid)
    ).one()
    assert updated.content_type == "html"
    assert updated.url is None
    assert updated.title is None
    assert isinstance(returned_ref, InlineHtmlRef)


def test_enrich_source_for_karakeep_ref_pre_resolves_to_refined_ref(db_session, monkeypatch):
    """F4/#29: parent enrichment pre-resolves a KarakeepBookmarkRef so the
    child subprocess sees the already-refined ref and doesn't re-fetch the
    bookmark. Verify:

    1. ``fetch_karakeep_bookmark`` is called exactly once (one RPC per job).
    2. ``_enrich_source_for_job`` returns the REFINED ref, not the original
       KarakeepBookmarkRef. That ref gets written to ``workspace/source_ref.json``
       and the child's orchestrator dispatches it directly to the terminal
       content fetcher — no resolver hop.
    3. The Source row's ``source_ref`` column stays the ORIGINAL
       KarakeepBookmarkRef (identity, immutable by the worker).
    """
    from aizk.conversion.workers.orchestrator import _enrich_source_for_job

    ref = KarakeepBookmarkRef(bookmark_id="bk-pre-resolve")
    original_payload = ref.model_dump()
    source = Source(
        karakeep_id="bk-pre-resolve",
        source_ref=original_payload,
        source_ref_hash=compute_source_ref_hash(ref),
    )
    db_session.add(source)
    db_session.commit()
    db_session.refresh(source)

    job = ConversionJob(
        aizk_uuid=source.aizk_uuid,
        source_ref=original_payload,
        title="initial",
        status=ConversionJobStatus.NEW,
        idempotency_key="k" * 64,
    )
    db_session.add(job)
    db_session.commit()

    # Bookmark stub: arxiv URL → resolver refines to ArxivRef.
    fake_bookmark = MagicMock()
    fake_bookmark.title = "A Paper"

    from aizk.conversion.workers import orchestrator as worker_orch

    calls: list[str] = []

    def _fake_fetch(bookmark_id, *, base_url=None, api_key=None):
        calls.append(bookmark_id)
        return fake_bookmark

    monkeypatch.setattr(worker_orch, "fetch_karakeep_bookmark", _fake_fetch)
    monkeypatch.setattr(worker_orch, "validate_bookmark_content", lambda _bm: None)
    monkeypatch.setattr(worker_orch, "detect_content_type", lambda _bm: "pdf")
    monkeypatch.setattr(worker_orch, "detect_source_type", lambda _url: "arxiv")
    monkeypatch.setattr(
        worker_orch, "get_bookmark_source_url", lambda _bm: "https://arxiv.org/abs/2401.00001"
    )

    # Stub the resolver's refine_from_bookmark via the class so we can verify
    # (a) it's called and (b) fetch_karakeep_bookmark is not invoked again from it.
    from aizk.conversion.adapters.fetchers.karakeep import KarakeepBookmarkResolver

    def _fake_refine(self, ref_in, bookmark_in):
        assert bookmark_in is fake_bookmark
        return ArxivRef(arxiv_id="2401.00001")

    monkeypatch.setattr(KarakeepBookmarkResolver, "refine_from_bookmark", _fake_refine)

    from aizk.conversion.utilities.config import ConversionConfig

    engine = db_session.get_bind()
    _, returned_ref = _enrich_source_for_job(
        job.id, engine, ConversionConfig(_env_file=None)
    )

    # 1. Single RPC
    assert calls == ["bk-pre-resolve"]

    # 2. Returned ref is the refined one, not the original KarakeepBookmarkRef
    assert isinstance(returned_ref, ArxivRef)
    assert returned_ref.arxiv_id == "2401.00001"

    # 3. Source.source_ref identity unchanged
    db_session.expire_all()
    updated = db_session.exec(
        select(Source).where(Source.aizk_uuid == source.aizk_uuid)
    ).one()
    assert updated.source_ref == original_payload


# ---------------------------------------------------------------------------
# _persist_artifacts — workspace layout matches uploader expectations
# ---------------------------------------------------------------------------


def test_persist_artifacts_writes_workspace_layout(tmp_path, monkeypatch):
    """Confirms the workspace after persist_artifacts is what the uploader expects."""
    from aizk.conversion.utilities.paths import (
        OUTPUT_MARKDOWN_FILENAME,
        figure_dir,
        markdown_path,
        metadata_path,
    )
    from aizk.conversion.workers.orchestrator import _persist_artifacts

    # Stub docling version lookup so the test doesn't require docling.
    monkeypatch.setattr(
        "aizk.conversion.workers.orchestrator._docling_version", lambda: "0.0.0-test"
    )

    fake_config = MagicMock()
    fake_config.is_picture_description_enabled.return_value = False
    # Return a dict matching what build_output_config_snapshot would produce.
    monkeypatch.setattr(
        "aizk.conversion.workers.orchestrator.build_output_config_snapshot",
        lambda cfg, *, picture_description_enabled: {"docling_stub": True},
    )

    artifacts = ConversionArtifacts(
        markdown="# hello\n\nbody text",
        figures=[b"\x89PNG\r\n\x1a\n...fake..."],
    )

    _persist_artifacts(
        workspace=tmp_path,
        artifacts=artifacts,
        content_type=ContentType.PDF,
        fetched_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        converter_name="docling",
        config=fake_config,
    )

    # Markdown present and normalized
    md = markdown_path(tmp_path, OUTPUT_MARKDOWN_FILENAME).read_text()
    assert "hello" in md

    # Figure written with deterministic name
    figs = list(figure_dir(tmp_path).iterdir())
    assert len(figs) == 1
    assert figs[0].name == "figure-000.png"

    # Metadata written with correct keys
    meta = json.loads(metadata_path(tmp_path).read_text())
    assert meta["pipeline_name"] == "pdf"
    assert meta["markdown_filename"] == OUTPUT_MARKDOWN_FILENAME
    assert meta["figure_files"] == ["figure-000.png"]
    assert meta["docling_version"] == "0.0.0-test"
    assert meta["converter_name"] == "docling"
    assert meta["config_snapshot"] == {"docling_stub": True}
    assert "markdown_hash_xx64" in meta
    assert meta["fetched_at"].startswith("2026-01-01")


def test_persist_artifacts_html_pipeline_name(tmp_path, monkeypatch):
    from aizk.conversion.utilities.paths import metadata_path
    from aizk.conversion.workers.orchestrator import _persist_artifacts

    monkeypatch.setattr(
        "aizk.conversion.workers.orchestrator._docling_version", lambda: "0.0.0-test"
    )
    monkeypatch.setattr(
        "aizk.conversion.workers.orchestrator.build_output_config_snapshot",
        lambda cfg, *, picture_description_enabled: {},
    )
    fake_config = MagicMock()
    fake_config.is_picture_description_enabled.return_value = False

    _persist_artifacts(
        workspace=tmp_path,
        artifacts=ConversionArtifacts(markdown="# html", figures=[]),
        content_type=ContentType.HTML,
        fetched_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        converter_name="docling",
        config=fake_config,
    )
    meta = json.loads(metadata_path(tmp_path).read_text())
    assert meta["pipeline_name"] == "html"
    assert meta["figure_files"] == []


# ---------------------------------------------------------------------------
# Fetchers populate source_url in ConversionInput.metadata
# ---------------------------------------------------------------------------


def test_url_fetcher_populates_source_url(monkeypatch):
    from aizk.conversion.adapters.fetchers.url import UrlFetcher

    class _FakeResp:
        content = b"<html></html>"
        headers = {"content-type": "text/html"}

        def raise_for_status(self):
            pass

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, url):
            return _FakeResp()

    monkeypatch.setattr("httpx.Client", _FakeClient)
    ref = UrlRef(url="https://example.com/page")
    result = UrlFetcher().fetch(ref)
    assert result.metadata.get("source_url") == "https://example.com/page"


def test_arxiv_fetcher_populates_abstract_source_url(monkeypatch):
    from aizk.conversion.adapters.fetchers.arxiv import ArxivFetcher

    class _FakeResp:
        content = b"%PDF-1.4\n..."

        def raise_for_status(self):
            pass

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, url):
            return _FakeResp()

    monkeypatch.setattr("httpx.Client", _FakeClient)
    ref = ArxivRef(
        arxiv_id="2301.12345",
        arxiv_pdf_url="https://arxiv.org/pdf/2301.12345.pdf",
    )
    result = ArxivFetcher().fetch(ref)
    assert result.metadata.get("source_url") == "https://arxiv.org/abs/2301.12345"
    assert result.metadata.get("arxiv_id") == "2301.12345"


# ---------------------------------------------------------------------------
# Idempotency key parity: API-persisted key matches independent computation
# ---------------------------------------------------------------------------


def test_api_idempotency_key_matches_independent_computation(db_session):
    """The idempotency_key persisted by the API equals a key independently computed
    from the same inputs (source_ref_hash, converter_name, config_snapshot).

    This catches silent divergence in the compute_idempotency_key formula — for
    example if the API route swaps argument order or changes the config snapshot
    schema without updating the formula.
    """
    from fastapi.testclient import TestClient

    from aizk.conversion.api.main import create_app
    from aizk.conversion.utilities.config import ConversionConfig
    from aizk.conversion.utilities.hashing import build_output_config_snapshot, compute_idempotency_key

    app = create_app()
    with TestClient(app) as client:
        resp = client.post(
            "/v1/jobs",
            json={"source_ref": {"kind": "karakeep_bookmark", "bookmark_id": "bm_idempotency_parity"}},
        )
    assert resp.status_code == 201
    api_idempotency_key = resp.json()["idempotency_key"]

    # Independently compute using the same inputs the route uses.
    ref = KarakeepBookmarkRef(bookmark_id="bm_idempotency_parity")
    source_ref_hash = compute_source_ref_hash(ref)
    cfg = ConversionConfig()
    config_snapshot = build_output_config_snapshot(
        cfg, picture_description_enabled=cfg.is_picture_description_enabled()
    )
    expected_key = compute_idempotency_key(
        source_ref_hash=source_ref_hash,
        converter_name="docling",  # ApiRuntime.converter_name default
        config_snapshot=config_snapshot,
    )
    assert api_idempotency_key == expected_key
