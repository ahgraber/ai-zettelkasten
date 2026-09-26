"""Tests for the contextualization worker composition root.

``run_graph_worker`` gates startup on the OpenAI-compatible endpoint triple before
anything runs, hands the resolved triple to the provider client builder, and wires
the stage's admission loop around the runner. ``build_llm_client`` constructs a
``pydantic-ai`` agent client from a resolved triple (no network call at
construction). Live model and S3 wiring are exercised only against a real
deployment, not here.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from aizk.ai.providers.llm import PydanticAILLMClient, build_llm_client
from aizk.contextualization.events import CONTEXTUALIZATION_STAGE
from aizk.contextualization.settings import ContextualizationConfig
from aizk.contextualization.wiring import worker as worker_module
from aizk.contextualization.wiring.worker import run_graph_worker
from aizk.pipeline.admission import AdmissionAdapter, AdmissionConfig
from aizk.utilities.errors import StartupValidationError


def _configured_config(**overrides: object) -> ContextualizationConfig:
    """Return a contextualization config whose model endpoint triple is complete."""
    return ContextualizationConfig(
        _env_file=None,
        llm_base_url="https://api.example.com/v1",
        llm_api_key="sk-test-unused",
        llm_model="openai/gpt-4.1-nano",
        **overrides,
    )


def test_build_llm_client_builds_a_pydantic_ai_client_when_configured() -> None:
    """A resolved triple yields a PydanticAILLMClient (constructed, not called)."""
    client = build_llm_client("https://openrouter.ai/api/v1", "sk-test-unused", "openai/gpt-4.1-nano")

    assert isinstance(client, PydanticAILLMClient)


class _RecordingAdmissionLoop:
    """Stands in for the real loop, recording construction and start/stop ordering."""

    instances: "list[_RecordingAdmissionLoop]" = []

    def __init__(self, engine: object, adapter: Any, *, interval_seconds: float) -> None:
        """Capture what the worker wired in and register this instance."""
        self.engine = engine
        self.adapter = adapter
        self.interval_seconds = interval_seconds
        self.events: list[str] = []
        _RecordingAdmissionLoop.instances.append(self)

    def __enter__(self) -> "_RecordingAdmissionLoop":
        """Record the start."""
        self.events.append("start")
        return self

    def __exit__(self, *_exc_info: object) -> None:
        """Record the stop."""
        self.events.append("stop")


@pytest.fixture
def stubbed_worker(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Neutralize every external dependency of ``run_graph_worker`` and record the loop order."""
    _RecordingAdmissionLoop.instances.clear()
    order: list[str] = []

    monkeypatch.setattr(worker_module, "DatabaseConfig", lambda: SimpleNamespace(database_url="sqlite://"))
    monkeypatch.setattr(worker_module, "get_engine", lambda _url: "fake-engine")
    monkeypatch.setattr(worker_module, "build_llm_client", lambda *_triple: object())
    monkeypatch.setattr(worker_module, "S3Client", lambda _config: object())
    monkeypatch.setattr(worker_module, "S3MarkdownSource", lambda _engine, _client: object())
    monkeypatch.setattr(worker_module, "ContextualizationStageHandler", lambda *_a, **_k: object())
    monkeypatch.setattr(worker_module, "AdmissionLoop", _RecordingAdmissionLoop)

    class _FakeRunner:
        def __init__(self, *_args: object, **_kwargs: object) -> None: ...

        def run(self) -> int:
            order.append("runner")
            return 0

    monkeypatch.setattr(worker_module, "StageRunner", _FakeRunner)
    return order


def test_the_worker_requires_a_configured_endpoint(stubbed_worker: list[str]) -> None:
    """An unconfigured model endpoint fails startup rather than running headless."""
    config = ContextualizationConfig(_env_file=None, llm_base_url="", llm_api_key="", llm_model="")
    assert config.llm_enabled is False

    with pytest.raises(StartupValidationError, match="model endpoint is not configured"):
        run_graph_worker(SimpleNamespace(), config)

    assert stubbed_worker == []
    assert _RecordingAdmissionLoop.instances == []


def test_the_worker_treats_an_unresolved_placeholder_as_unconfigured(stubbed_worker: list[str]) -> None:
    """A triple still holding a ``${...}`` placeholder is treated as unconfigured.

    An unexpanded ``.env`` interpolation is non-empty but invalid; it must fail the
    startup gate rather than slip through and fail only once a job calls the model.
    """
    config = ContextualizationConfig(
        _env_file=None,
        llm_base_url="${_OPENROUTER_BASE_URL}",
        llm_api_key="${_OPENROUTER_API_KEY}",
        llm_model="openai/gpt-4.1-nano",
    )
    assert config.llm_enabled is False

    with pytest.raises(StartupValidationError, match="model endpoint is not configured"):
        run_graph_worker(SimpleNamespace(), config)

    assert stubbed_worker == []
    assert _RecordingAdmissionLoop.instances == []


def test_the_worker_hosts_the_stages_admission_loop(stubbed_worker: list[str]) -> None:
    """The worker runs the contextualization stage's admission alongside its claim loop."""
    admission_config = AdmissionConfig(
        _env_file=None,
        admission_contextualization_enabled=True,
        contextualization_queue_max_depth=25,
        admission_interval_seconds=7.5,
    )

    assert run_graph_worker(SimpleNamespace(), _configured_config(), admission_config) == 0

    (loop,) = _RecordingAdmissionLoop.instances
    assert loop.adapter.stage == CONTEXTUALIZATION_STAGE
    assert loop.adapter.enabled is True
    assert loop.adapter.queue_max_depth == 25
    assert loop.interval_seconds == 7.5


def test_the_worker_stops_the_admission_loop_after_the_runner_returns(stubbed_worker: list[str]) -> None:
    """Admission starts before the claim loop and is stopped once it returns, leaving nothing running."""
    run_graph_worker(SimpleNamespace(), _configured_config(), AdmissionConfig(_env_file=None))

    (loop,) = _RecordingAdmissionLoop.instances
    assert loop.events == ["start", "stop"]
    assert stubbed_worker == ["runner"]


def test_the_worker_reads_admission_settings_when_none_are_supplied(stubbed_worker: list[str]) -> None:
    """A caller that supplies no admission config gets the environment's, defaulting to off."""
    run_graph_worker(SimpleNamespace(), _configured_config())

    (loop,) = _RecordingAdmissionLoop.instances
    assert loop.adapter.enabled is False


def test_the_worker_hands_the_loop_the_adapter_contextualization_adapter_returns(
    stubbed_worker: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loop receives the adapter object ``contextualization_adapter`` returns for this stage."""
    resolved = AdmissionAdapter(
        stage=CONTEXTUALIZATION_STAGE,
        model=object(),
        pending_work=lambda *_a, **_k: [],
        enqueue=lambda *_a, **_k: None,
        queue_max_depth=99,
        enabled=True,
    )
    monkeypatch.setattr(worker_module, "contextualization_adapter", lambda _config: resolved)

    run_graph_worker(SimpleNamespace(), _configured_config())

    (loop,) = _RecordingAdmissionLoop.instances
    assert loop.adapter is resolved
