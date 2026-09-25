"""One fixtured KaraKeep bookmark carried from the conversion API through conversion, graph intake, contextualization, and extraction."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import nullcontext
from pathlib import Path
import socket
from typing import Any

import pytest
from sqlalchemy import Engine
from sqlmodel import Session, select

from fastapi.testclient import TestClient

from aizk.ai.providers.llm import STUB_MODEL_CONFIG, StubLLMClient
from aizk.conversion.adapters.fetchers.arxiv import ArxivFetcher
from aizk.conversion.adapters.fetchers.github import GithubReadmeFetcher
from aizk.conversion.api.main import create_app
from aizk.conversion.core.coordinator import ConversionCoordinator
from aizk.conversion.core.registry import ConverterRegistry, FetcherRegistry
from aizk.conversion.core.source_ref import ArxivRef, GithubReadmeRef, SourceRef
from aizk.conversion.core.types import SourceMetadata
from aizk.conversion.datamodel.job import ConversionJob
from aizk.conversion.datamodel.output import ConversionOutput
from aizk.conversion.datamodel.source import Source
from aizk.conversion.handler import ConversionStageHandler
from aizk.conversion.settings import ConversionConfig, KarakeepFetcherConfig
from aizk.conversion.wiring import subprocess as wiring_subprocess
from aizk.conversion.wiring.capabilities import DeploymentCapabilities
from aizk.conversion.wiring.registrations import register_fetchers
from aizk.conversion.wiring.worker import WorkerRuntime
from aizk.db.engine import _ENGINE_CACHE, get_engine
from aizk.db.migrations import run_migrations
from aizk.graph.chunks import active_chunking_run
from aizk.graph.contextualization import VARIANT_STAGE
from aizk.graph.datamodel import Chunk, ChunkRunInput, ContextualizationJob, ContextualizedChunk, Mention
from aizk.graph.enqueue import enqueue_output
from aizk.graph.extraction_handler import ExtractionStageHandler
from aizk.graph.extraction_workunit import enqueue_extraction
from aizk.graph.handler import ContextualizationStageHandler
from aizk.graph.markdown_source import ConversionOutputFreshness, S3MarkdownSource
from aizk.graph.mentions import INPUT_POLICY_RAW
from aizk.pipeline.lifecycle import WorkUnitStatus
from aizk.pipeline.run import active_run
from aizk.storage.settings import ObjectStorageSettings
from aizk.utilities.hashing import compute_markdown_hash
from tests.fakes import FIXTURES_DIR
from tests.fakes.converter import FixturedDoclingConverter
from tests.fakes.extraction import KeywordExtractor
from tests.fakes.http import (
    ARXIV_ID,
    GITHUB_OWNER,
    GITHUB_REPO,
    KARAKEEP_BASE_URL,
    KARAKEEP_BOOKMARK_ID,
    install_fixtured_egress,
)
from tests.fakes.process import InlineContext
from tests.fakes.storage import MemoryS3, install_memory_s3

_PROPER_NOUNS = ("Ada Lovelace", "Charles Babbage")


@pytest.fixture
def walkthrough_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    """Point every settings class at the test database, the in-memory store, and the fixture KaraKeep host."""
    database_url = f"sqlite:///{tmp_path / 'walkthrough.db'}"
    monkeypatch.setenv("AIZK_DATABASE_URL", database_url)
    monkeypatch.setenv("AIZK_TRUSTED_HOSTS", '["testserver", "localhost", "127.0.0.1"]')
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AIZK_S3_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AIZK_S3_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AIZK_S3_REGION", "us-east-1")
    monkeypatch.setenv("AIZK_S3_BUCKET_NAME", "test-bucket")
    monkeypatch.setenv("AIZK_S3_ENDPOINT_URL", "http://localhost:9000")
    monkeypatch.setenv("AIZK_RETRY_BASE_DELAY_SECONDS", "0")
    monkeypatch.setenv("AIZK_FETCHER__KARAKEEP__BASE_URL", KARAKEEP_BASE_URL)
    monkeypatch.setenv("AIZK_FETCHER__KARAKEEP__API_KEY", "test")
    return database_url


@pytest.fixture
def engine(walkthrough_env: str) -> Iterator[Engine]:
    """A migration-built engine on the test database, dropped from the engine cache at teardown."""
    run_migrations(walkthrough_env)
    engine = get_engine(walkthrough_env)
    try:
        yield engine
    finally:
        engine.dispose()
        _ENGINE_CACHE.pop(walkthrough_env, None)


@pytest.fixture
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve every host to a public IP so the egress policy admits the fixture hosts."""

    def fake(host: str, port: int, *_args: Any, **_kwargs: Any) -> list[tuple[Any, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test the moment a socket connects or a boto3 client is constructed."""

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("the walkthrough must not open a socket or construct a boto3 client")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr("boto3.client", refuse)


def _build_runtime(config: ConversionConfig, converter: FixturedDoclingConverter) -> WorkerRuntime:
    """Assemble a worker runtime over the real fetchers and the fixtured converter alone."""
    fetcher_registry = FetcherRegistry()
    register_fetchers(fetcher_registry, config, karakeep_cfg=KarakeepFetcherConfig(_env_file=None))
    converter_registry = ConverterRegistry()
    converter_registry.register(converter, config.worker_converter_name)
    return WorkerRuntime(
        coordinator=ConversionCoordinator(
            resolve_fetcher=fetcher_registry.resolve, resolve_converter=converter_registry.resolve
        ),
        resource_guard=nullcontext(),
        capabilities=DeploymentCapabilities(fetcher_registry, converter_registry, startup_probes=[]),
        object_storage=ObjectStorageSettings(_env_file=None),
        spawn_subprocess=wiring_subprocess._spawn_conversion_subprocess,
    )


def _run_unit(handler: Any, engine: Engine) -> int:
    """Claim the next unit, execute it, and finalize its outcome the way the runner does; return its handle."""
    with Session(engine) as session:
        handle = handler.claim_next(session)
        session.commit()
    assert handle is not None
    outcome = handler.map_result(handler.execute(handle))
    with Session(engine) as session:
        handler.finalize(session, handle, outcome)
        session.commit()
    return handle


def test_bookmark_flows_from_submission_to_mentions(
    engine: Engine, public_dns: None, no_network: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Submit the fixture bookmark to the API, convert it in-process through the real child entry, enqueue and run its contextualization unit, then enqueue and run its extraction unit; assert the output row and blob, the enqueued unit's source, the chunk rows and their recorded hash, one variant per chunk, and the mention rows."""
    store = install_memory_s3(monkeypatch)
    install_fixtured_egress(monkeypatch)
    config = ConversionConfig(_env_file=None)
    converter = FixturedDoclingConverter()
    runtime = _build_runtime(config, converter)
    monkeypatch.setattr(wiring_subprocess.mp, "get_context", lambda _ctx: InlineContext())
    monkeypatch.setattr(wiring_subprocess.os, "setpgrp", lambda: None)
    monkeypatch.setattr(wiring_subprocess, "set_process_title", lambda _title: None)
    monkeypatch.setattr(wiring_subprocess, "load_process_dotenv_once", lambda: None)
    monkeypatch.setattr("aizk.conversion.api.main.load_process_dotenv_once", lambda: None)
    monkeypatch.setattr("aizk.conversion.wiring.worker.build_worker_runtime", lambda _cfg, **_kwargs: runtime)

    with TestClient(create_app()) as client:
        response = client.post(
            "/v1/jobs", json={"source_ref": {"kind": "karakeep_bookmark", "bookmark_id": KARAKEEP_BOOKMARK_ID}}
        )
    assert response.status_code == 201
    job_id = response.json()["id"]
    with Session(engine) as session:
        source_id = session.exec(select(Source).where(Source.karakeep_id == KARAKEEP_BOOKMARK_ID)).one().source_id

    handle = _run_unit(ConversionStageHandler(config, runtime=runtime), engine)
    assert handle == job_id
    assert converter.inputs[0].content == (FIXTURES_DIR / "article.html").read_bytes()
    with Session(engine) as session:
        assert session.get(ConversionJob, job_id).status == WorkUnitStatus.SUCCEEDED
        assert session.get(ConversionJob, job_id).phase is None
        output = session.exec(select(ConversionOutput).where(ConversionOutput.job_id == job_id)).one()
        output_id, markdown_key, markdown_hash = output.id, output.markdown_key, output.markdown_hash_xx64
    assert markdown_key in store
    assert markdown_hash == compute_markdown_hash(store[markdown_key].decode("utf-8"))

    with Session(engine) as session:
        enqueue_output(session, output_id, queue_max_depth=0, origin="intake", principal="test")
        session.commit()
        assert [unit.source_id for unit in session.exec(select(ContextualizationJob)).all()] == [source_id]

    _run_unit(
        ContextualizationStageHandler(
            engine,
            StubLLMClient(),
            S3MarkdownSource(engine, MemoryS3(store)),
            ConversionOutputFreshness(),
            model_config=STUB_MODEL_CONFIG,
        ),
        engine,
    )
    with Session(engine) as session:
        chunk_ids = set(session.exec(select(Chunk.chunk_id).where(Chunk.source_id == source_id)).all())
        assert chunk_ids
        assert (
            session.get(ChunkRunInput, active_chunking_run(session, source_id).id).markdown_hash_xx64 == markdown_hash
        )
        variant_run = active_run(session, VARIANT_STAGE, source_id)
        variants = session.exec(select(ContextualizedChunk).where(ContextualizedChunk.run_id == variant_run.id)).all()
        assert sorted(variant.chunk_id for variant in variants) == sorted(chunk_ids)

    with Session(engine) as session:
        enqueue_extraction(session, source_id=source_id, queue_max_depth=0, origin="intake", principal="test")
        session.commit()
    _run_unit(ExtractionStageHandler(engine, KeywordExtractor(_PROPER_NOUNS), input_policy=INPUT_POLICY_RAW), engine)
    with Session(engine) as session:
        mentions = session.exec(select(Mention)).all()
        assert mentions
        assert {mention.chunk_id for mention in mentions} <= chunk_ids


@pytest.mark.parametrize(
    ("build_fetcher", "ref", "fixture_name"),
    [
        pytest.param(
            lambda config: ArxivFetcher(config, KarakeepFetcherConfig(_env_file=None)),
            ArxivRef(arxiv_id=ARXIV_ID),
            "arxiv.pdf",
            id="arxiv",
        ),
        pytest.param(
            lambda config: GithubReadmeFetcher(config),
            GithubReadmeRef(owner=GITHUB_OWNER, repo=GITHUB_REPO),
            "README.md",
            id="github",
        ),
    ],
)
def test_fixtured_fetchers_return_the_fixture_bytes(
    public_dns: None,
    no_network: None,
    monkeypatch: pytest.MonkeyPatch,
    build_fetcher: Callable[[ConversionConfig], Any],
    ref: SourceRef,
    fixture_name: str,
) -> None:
    """The real arXiv and GitHub fetchers, over the fixtured transport, return the fixture file's bytes."""
    install_fixtured_egress(monkeypatch)
    fetched = build_fetcher(ConversionConfig(_env_file=None)).fetch(ref, SourceMetadata())
    assert fetched.content == (FIXTURES_DIR / fixture_name).read_bytes()
