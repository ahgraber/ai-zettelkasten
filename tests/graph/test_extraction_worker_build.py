"""Tests for the extraction worker composition root.

``build_extractor`` selects the pinned :class:`~aizk.graph.extraction.EntityExtractor`
implementation named by ``ExtractionConfig.extractor``, and
``run_extraction_worker`` wires the stage's admission loop around the runner.
Construction is patched at the composition root's import location (not exercised
for real) so the suite stays hermetic and fast, mirroring
``tests/graph/test_worker_build.py``'s treatment of the contextualization
worker.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from pydantic import ValidationError
import pytest

from aizk.graph import extraction_worker as extraction_worker_module
from aizk.graph.config import AdmissionConfig, ExtractionConfig
from aizk.graph.extraction_events import EXTRACTION_STAGE
from aizk.graph.extraction_worker import build_extractor, run_extraction_worker


def test_build_extractor_selects_spacy(monkeypatch: pytest.MonkeyPatch) -> None:
    """``extractor = "spacy"`` constructs a SpacyExtractor."""
    sentinel = object()
    monkeypatch.setattr("aizk.graph.extraction_worker.SpacyExtractor", lambda: sentinel)
    config = ExtractionConfig(extractor="spacy")

    assert build_extractor(config) is sentinel


def test_build_extractor_selects_gliner2(monkeypatch: pytest.MonkeyPatch) -> None:
    """``extractor = "gliner2"`` constructs a Gliner2Extractor."""
    sentinel = object()
    monkeypatch.setattr("aizk.graph.extraction_worker.Gliner2Extractor", lambda: sentinel)
    config = ExtractionConfig(extractor="gliner2")

    assert build_extractor(config) is sentinel


def test_extraction_config_defaults() -> None:
    """The default config selects spaCy and the contextualized input policy."""
    config = ExtractionConfig()

    assert config.extractor == "spacy"
    assert config.input_policy == "contextualized"


def test_extraction_config_rejects_unknown_extractor() -> None:
    """An extractor value outside {spacy, gliner2} is rejected at config-parse time."""
    with pytest.raises(ValidationError):
        ExtractionConfig(extractor="unknown")  # type: ignore[arg-type]


def test_extraction_config_rejects_unknown_input_policy() -> None:
    """An input_policy value outside {contextualized, raw} is rejected at config-parse time."""
    with pytest.raises(ValidationError):
        ExtractionConfig(input_policy="unknown")  # type: ignore[arg-type]


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
    """Neutralize every external dependency of ``run_extraction_worker`` and record the loop order."""
    _RecordingAdmissionLoop.instances.clear()
    order: list[str] = []

    monkeypatch.setattr(extraction_worker_module, "DatabaseConfig", lambda: SimpleNamespace(database_url="sqlite://"))
    monkeypatch.setattr(extraction_worker_module, "get_engine", lambda _url: "fake-engine")
    monkeypatch.setattr(extraction_worker_module, "build_extractor", lambda _config: object())
    monkeypatch.setattr(extraction_worker_module, "ExtractionStageHandler", lambda *_a, **_k: object())
    monkeypatch.setattr(extraction_worker_module, "AdmissionLoop", _RecordingAdmissionLoop)

    class _FakeRunner:
        def __init__(self, *_args: object, **_kwargs: object) -> None: ...

        def run(self) -> int:
            order.append("runner")
            return 0

    monkeypatch.setattr(extraction_worker_module, "StageRunner", _FakeRunner)
    return order


def test_the_worker_hosts_the_stages_admission_loop(stubbed_worker: list[str]) -> None:
    """The worker runs the extraction stage's admission alongside its claim loop."""
    admission_config = AdmissionConfig(
        _env_file=None,
        admission_extraction_enabled=True,
        extraction_queue_max_depth=40,
        admission_interval_seconds=12.0,
    )

    assert run_extraction_worker(ExtractionConfig(_env_file=None), admission_config) == 0

    (loop,) = _RecordingAdmissionLoop.instances
    assert loop.adapter.stage == EXTRACTION_STAGE
    assert loop.adapter.enabled is True
    assert loop.adapter.queue_max_depth == 40
    assert loop.interval_seconds == 12.0


def test_the_worker_stops_the_admission_loop_after_the_runner_returns(stubbed_worker: list[str]) -> None:
    """Admission starts before the claim loop and is stopped once it returns, leaving nothing running."""
    run_extraction_worker(ExtractionConfig(_env_file=None), AdmissionConfig(_env_file=None))

    (loop,) = _RecordingAdmissionLoop.instances
    assert loop.events == ["start", "stop"]
    assert stubbed_worker == ["runner"]


def test_the_worker_reads_admission_settings_when_none_are_supplied(stubbed_worker: list[str]) -> None:
    """A caller that supplies no admission config gets the environment's, defaulting to off."""
    run_extraction_worker(ExtractionConfig(_env_file=None))

    (loop,) = _RecordingAdmissionLoop.instances
    assert loop.adapter.enabled is False
