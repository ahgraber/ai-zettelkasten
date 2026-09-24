"""Tests for the contextualization stage definition and its runner-facing identity."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from sqlalchemy import Engine
from sqlmodel import Session

from aizk.contextualization.handler import ContextualizationStageHandler
from aizk.contextualization.processing.markdown_source import ConversionOutputFreshness
from aizk.contextualization.stage import CONTEXTUALIZATION_DEFINITION, ContextualizationParams
from aizk.conversion.datamodel.output import ConversionOutput
from aizk.conversion.datamodel.source import Source
from aizk.graph.llm import STUB_MODEL_CONFIG, StubLLMClient
import aizk.pipeline.runner as runner_module
from aizk.pipeline.runner import StageRunner
from tests.fakes.markdown import StubMarkdownSource


def test_definition_pending_returns_the_params_of_a_pending_output(
    session: Session,
    seed_source: Callable[..., Source],
    seed_conversion_output: Callable[..., ConversionOutput],
) -> None:
    """The definition's pending query yields the parameters of an output with no work-unit."""
    source = seed_source(session, karakeep_id="pending-doc")
    output = seed_conversion_output(session, job_id=1, source_id=source.source_id)

    pending = CONTEXTUALIZATION_DEFINITION.pending(session, None)

    assert pending == [ContextualizationParams(conversion_output_id=output.id, source_id=source.source_id)]


def test_the_runner_titles_the_process_after_the_contextualization_stage(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A runner driving the contextualization handler advertises ``aizk-stage-contextualization``."""
    titles: list[str] = []
    monkeypatch.setattr(runner_module, "setproctitle", lambda title: titles.append(title))
    handler = ContextualizationStageHandler(
        engine, StubLLMClient(), StubMarkdownSource(), ConversionOutputFreshness(), model_config=STUB_MODEL_CONFIG
    )

    StageRunner(handler, engine).set_process_title()

    assert titles == ["aizk-stage-contextualization"]
