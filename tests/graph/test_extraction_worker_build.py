"""Tests for the extraction worker composition root (extractor selection).

``build_extractor`` selects the pinned :class:`~aizk.graph.extraction.EntityExtractor`
implementation named by ``ExtractionConfig.extractor``. Construction is patched
at the composition root's import location (not exercised for real) so the
suite stays hermetic and fast, mirroring
``tests/graph/test_worker_build.py``'s treatment of the contextualization
worker's model-client build.
"""

from __future__ import annotations

from pydantic import ValidationError
import pytest

from aizk.graph.config import ExtractionConfig
from aizk.graph.extraction_worker import build_extractor


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
