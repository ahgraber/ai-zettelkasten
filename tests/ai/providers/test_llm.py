"""Tests for the model access point (:mod:`aizk.ai.providers.llm`) and its resolved-config fingerprint.

Covers :class:`ModelConfig` — the value object standing in for "which model
produced this output" in a derivation key — and :func:`model_config_fingerprint`:
two configs that resolve to the same provider/model/settings fingerprint equal
regardless of what alias named them, and any resolved difference changes the
fingerprint.
"""

from __future__ import annotations

from aizk.ai.providers.llm import STUB_MODEL_CONFIG, ModelConfig, model_config_fingerprint
from aizk.graph.config import ContextualizationConfig


def test_different_provider_or_model_changes_the_fingerprint() -> None:
    """A different ``provider`` or ``model`` changes ``model_config_fingerprint``."""
    base = ModelConfig(provider="https://api.example.com/v1", model="gpt-4o")
    different_provider = ModelConfig(provider="https://other.example.com/v1", model="gpt-4o")
    different_model = ModelConfig(provider="https://api.example.com/v1", model="gpt-4o-mini")

    assert model_config_fingerprint(different_provider) != model_config_fingerprint(base)
    assert model_config_fingerprint(different_model) != model_config_fingerprint(base)


def test_from_contextualization_config_reads_base_url_and_model() -> None:
    """``from_contextualization_config`` resolves ``provider``/``model`` from ``llm_base_url``/``llm_model``."""
    config = ContextualizationConfig(
        _env_file=None,
        llm_base_url="https://api.example.com/v1",
        llm_api_key="secret",
        llm_model="gpt-4o",
    )
    model_config = ModelConfig.from_contextualization_config(config)
    assert model_config.provider == "https://api.example.com/v1"
    assert model_config.model == "gpt-4o"


def test_profile_alias_change_with_unchanged_resolution_keeps_the_key() -> None:
    """Two configs that resolve to the same base URL and model fingerprint equal.

    The alias/API-key a caller used to reach the endpoint is not part of the
    resolved identity: only the base URL and model name determine
    ``model_config_fingerprint``, so swapping the API key alone (a profile alias
    pointed at the same endpoint) never changes the fingerprint.
    """
    config_a = ContextualizationConfig(
        _env_file=None, llm_base_url="https://api.example.com/v1", llm_api_key="key-a", llm_model="gpt-4o"
    )
    config_b = ContextualizationConfig(
        _env_file=None, llm_base_url="https://api.example.com/v1", llm_api_key="key-b", llm_model="gpt-4o"
    )
    fingerprint_a = model_config_fingerprint(ModelConfig.from_contextualization_config(config_a))
    fingerprint_b = model_config_fingerprint(ModelConfig.from_contextualization_config(config_b))
    assert fingerprint_a == fingerprint_b, "an unchanged resolution must keep the fingerprint, regardless of alias"


def test_changed_resolution_under_the_same_alias_changes_it() -> None:
    """Same everything but ``llm_model`` differs: the fingerprint changes."""
    config_a = ContextualizationConfig(
        _env_file=None, llm_base_url="https://api.example.com/v1", llm_api_key="key-a", llm_model="gpt-4o"
    )
    config_b = ContextualizationConfig(
        _env_file=None, llm_base_url="https://api.example.com/v1", llm_api_key="key-a", llm_model="gpt-4o-mini"
    )
    fingerprint_a = model_config_fingerprint(ModelConfig.from_contextualization_config(config_a))
    fingerprint_b = model_config_fingerprint(ModelConfig.from_contextualization_config(config_b))
    assert fingerprint_a != fingerprint_b, "a changed resolution must change the fingerprint"


def test_stub_model_config_is_a_model_config() -> None:
    """``STUB_MODEL_CONFIG`` is a usable ``ModelConfig`` for tests."""
    assert isinstance(STUB_MODEL_CONFIG, ModelConfig)
    model_config_fingerprint(STUB_MODEL_CONFIG)  # does not raise
