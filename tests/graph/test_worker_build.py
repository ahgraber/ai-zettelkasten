"""Tests for the contextualization worker composition root (model-client build).

``build_llm_client`` gates startup on the OpenAI-compatible endpoint triple and,
when configured, constructs a ``pydantic-ai`` agent client (no network call at
construction). Live model and S3 wiring are exercised only against a real
deployment, not here.
"""

from __future__ import annotations

import pytest

from aizk.conversion.utilities.startup import StartupValidationError
from aizk.graph.config import ContextualizationConfig
from aizk.graph.llm import PydanticAILLMClient
from aizk.graph.worker import build_llm_client


def test_build_llm_client_requires_configured_endpoint() -> None:
    """An unconfigured model endpoint fails startup rather than running headless."""
    config = ContextualizationConfig(llm_base_url="", llm_api_key="", llm_model="")
    assert config.llm_enabled is False
    with pytest.raises(StartupValidationError, match="model endpoint is not configured"):
        build_llm_client(config)


def test_unresolved_placeholder_counts_as_unconfigured() -> None:
    """A triple still holding a ``${...}`` placeholder is treated as unconfigured.

    An unexpanded ``.env`` interpolation is non-empty but invalid; it must fail the
    startup gate rather than slip through and fail only once a job calls the model.
    """
    config = ContextualizationConfig(
        llm_base_url="${_OPENROUTER_BASE_URL}",
        llm_api_key="${_OPENROUTER_API_KEY}",
        llm_model="openai/gpt-4.1-nano",
    )
    assert config.llm_enabled is False
    with pytest.raises(StartupValidationError, match="model endpoint is not configured"):
        build_llm_client(config)


def test_build_llm_client_builds_a_pydantic_ai_client_when_configured() -> None:
    """A fully configured triple yields a PydanticAILLMClient (constructed, not called)."""
    config = ContextualizationConfig(
        llm_base_url="https://openrouter.ai/api/v1",
        llm_api_key="sk-test-unused",
        llm_model="openai/gpt-4.1-nano",
    )
    assert config.llm_enabled is True

    client = build_llm_client(config)

    assert isinstance(client, PydanticAILLMClient)
