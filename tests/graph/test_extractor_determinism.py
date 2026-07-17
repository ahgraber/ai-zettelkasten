"""Determinism gate for the pinned production entity extractors.

Retry idempotency in the mention store rests on an assumption this module
verifies rather than assumes: two invocations of a pinned extractor on
identical text yield identical detection sequences (design decision
``TwoPinnedExtractorsSpacyAndGliner2``). spaCy is deterministic by
construction; GLiNER2's determinism is verified here empirically. Each test is
skipped cleanly at collection time when its extractor's dependencies or model
artifacts are unavailable, so the broad test suite never requires the opt-in
``ner`` dependency group or a network fetch.

The gate is opt-in — run it explicitly and serially::

    AIZK_RUN_NER_DETERMINISM=1 uv run pytest tests/graph/test_extractor_determinism.py

and re-run it after any extractor, model, or pinned-revision bump. It does not
run in the default suite even when the models are installed: loading a
gigabyte-scale torch model per test is slow, and under ``pytest -n auto`` the
cross-worker CPU/memory contention makes real-model inference flaky in ways
that say nothing about the determinism this gate exists to verify.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

from aizk.graph.config import NerConfig

pytestmark = pytest.mark.skipif(
    os.environ.get("AIZK_RUN_NER_DETERMINISM") != "1",
    reason="opt-in real-model gate: set AIZK_RUN_NER_DETERMINISM=1 and run this file serially",
)


def _spacy_available() -> bool:
    """Return whether spaCy and its pinned ``en_core_web_sm`` pipeline can be loaded."""
    if importlib.util.find_spec("spacy") is None:
        return False
    import spacy

    try:
        spacy.load("en_core_web_sm")
    except OSError:
        return False
    return True


def _gliner2_available() -> bool:
    """Return whether ``gliner2[local]`` is installed and its pinned local weights are present."""
    if importlib.util.find_spec("gliner2") is None:
        return False
    return Path(NerConfig().gliner2_model_dir).exists()


_SPACY_TEXT = "Apple is looking at buying U.K. startup for $1 billion. Tim Cook confirmed the plan in Cupertino."
_GLINER2_TEXT = "Apple CEO Tim Cook announced the iPhone 15 in Cupertino yesterday."


@pytest.mark.skipif(not _spacy_available(), reason="spaCy / en_core_web_sm not installed (uv sync --group ner)")
def test_spacy_extractor_is_deterministic() -> None:
    """Two SpacyExtractor invocations on identical text yield identical detection sequences."""
    from aizk.graph.extraction import SpacyExtractor

    extractor = SpacyExtractor()

    first = extractor.extract(_SPACY_TEXT)
    second = extractor.extract(_SPACY_TEXT)

    assert first == second
    assert len(first) > 0, "the fixture text must exercise at least one detection"


@pytest.mark.skipif(
    not _gliner2_available(),
    reason="GLiNER2 local weights not fetched (aizk-graph fetch-gliner2-weights)",
)
def test_gliner2_extractor_is_deterministic() -> None:
    """Two Gliner2Extractor invocations on identical text yield identical detection sequences."""
    from aizk.graph.extraction import Gliner2Extractor

    extractor = Gliner2Extractor()

    first = extractor.extract(_GLINER2_TEXT)
    second = extractor.extract(_GLINER2_TEXT)

    assert first == second
    assert len(first) > 0, "the fixture text must exercise at least one detection"
