"""Unit tests for the graph stages' admission and capacity settings.

`AdmissionConfig` is the graph-level section governing what may enter the two
stages' queues. Every construction passes `_env_file=None`, so the tests read
only the environment the test itself sets and never the developer's `.env`.
"""

from __future__ import annotations

import pytest

from aizk.graph.config import AdmissionConfig


def test_capacity_limits_default_to_no_limit() -> None:
    """An unconfigured deployment declares no capacity limit for either stage."""
    config = AdmissionConfig(_env_file=None)

    assert config.contextualization_queue_max_depth == 0
    assert config.extraction_queue_max_depth == 0


def test_queue_retry_after_matches_the_conversion_default() -> None:
    """The refusal delay carries the same default the conversion service reports."""
    assert AdmissionConfig(_env_file=None).queue_retry_after_seconds == 30


@pytest.mark.parametrize(
    ("env_var", "field"),
    [
        ("AIZK_GRAPH__CONTEXTUALIZATION_QUEUE_MAX_DEPTH", "contextualization_queue_max_depth"),
        ("AIZK_GRAPH__EXTRACTION_QUEUE_MAX_DEPTH", "extraction_queue_max_depth"),
        ("AIZK_GRAPH__QUEUE_RETRY_AFTER_SECONDS", "queue_retry_after_seconds"),
        ("AIZK_GRAPH__ADMISSION_INTERVAL_SECONDS", "admission_interval_seconds"),
    ],
)
def test_each_field_reads_its_own_environment_variable(
    monkeypatch: pytest.MonkeyPatch, env_var: str, field: str
) -> None:
    """Each setting is reachable under the documented `AIZK_GRAPH__` variable name."""
    monkeypatch.setenv(env_var, "17")

    assert getattr(AdmissionConfig(_env_file=None), field) == 17


def test_automatic_admission_is_off_by_default() -> None:
    """Nothing is admitted automatically until a deployment switches a stage on."""
    config = AdmissionConfig(_env_file=None)

    assert config.admission_contextualization_enabled is False
    assert config.admission_extraction_enabled is False


@pytest.mark.parametrize(
    ("env_var", "enabled_field", "other_field"),
    [
        (
            "AIZK_GRAPH__ADMISSION_CONTEXTUALIZATION_ENABLED",
            "admission_contextualization_enabled",
            "admission_extraction_enabled",
        ),
        (
            "AIZK_GRAPH__ADMISSION_EXTRACTION_ENABLED",
            "admission_extraction_enabled",
            "admission_contextualization_enabled",
        ),
    ],
    ids=["contextualization", "extraction"],
)
def test_enabling_one_stage_does_not_enable_the_other(
    monkeypatch: pytest.MonkeyPatch, env_var: str, enabled_field: str, other_field: str
) -> None:
    """Automatic admission is switched per stage, so their spend decisions stay independent."""
    monkeypatch.setenv(env_var, "true")

    config = AdmissionConfig(_env_file=None)

    assert getattr(config, enabled_field) is True
    assert getattr(config, other_field) is False


def test_the_two_stage_limits_are_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Limiting one stage leaves the other unlimited, so their spend profiles stay separate."""
    monkeypatch.setenv("AIZK_GRAPH__CONTEXTUALIZATION_QUEUE_MAX_DEPTH", "5")

    config = AdmissionConfig(_env_file=None)

    assert config.contextualization_queue_max_depth == 5
    assert config.extraction_queue_max_depth == 0
