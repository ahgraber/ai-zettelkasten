"""Tests for the service-wide settings.

``ServiceSettings`` is the one reader of these six ``AIZK_*`` names.
"""

from __future__ import annotations

from dotenv import load_dotenv
import pytest

from aizk.service.settings import ServiceSettings

#: The names ``ServiceSettings`` reads, paired with the field each lands on
#: and a value distinct from the shipped default.
SHARED_VARIABLES: list[tuple[str, str, str]] = [
    ("AIZK_LOG_LEVEL", "log_level", "DEBUG"),
    ("AIZK_LOG_FORMAT", "log_format", "text"),
    ("AIZK_API_HOST", "api_host", "127.0.0.1"),
    ("AIZK_API_PORT", "api_port", "8123"),
    ("AIZK_API_RELOAD", "api_reload", "true"),
    ("AIZK_TRUSTED_HOSTS", "trusted_hosts", '["a.internal"]'),
]


@pytest.fixture()
def _shared_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set every shared variable to a value distinct from its shipped default."""
    for name, _field, value in SHARED_VARIABLES:
        monkeypatch.setenv(name, value)


@pytest.mark.usefixtures("_shared_environment")
def test_service_settings_reads_the_shared_names() -> None:
    """The service settings read every environment name this environment sets."""
    service = ServiceSettings()

    assert service.trusted_hosts == ["a.internal"]
    assert service.api_port == 8123
    assert service.api_reload is True


def test_mlflow_section_reads_its_own_environment_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tracing section is configured by the fully spelled ``AIZK_MLFLOW__*`` names."""
    monkeypatch.setenv("AIZK_MLFLOW__ENABLED", "true")
    monkeypatch.setenv("AIZK_MLFLOW__TRACKING_URI", "http://mlflow.internal:5000")
    monkeypatch.setenv("AIZK_MLFLOW__EXPERIMENT_NAME", "aizk-conversion")

    mlflow = ServiceSettings().mlflow

    assert (mlflow.enabled, mlflow.tracking_uri, mlflow.experiment_name) == (
        True,
        "http://mlflow.internal:5000",
        "aizk-conversion",
    )


def test_operator_api_section_reads_its_own_environment_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """The operator listener is configured by the fully spelled ``AIZK_SERVICE__OPERATOR_API_*`` names."""
    monkeypatch.setenv("AIZK_SERVICE__OPERATOR_API_HOST", "127.0.0.1")
    monkeypatch.setenv("AIZK_SERVICE__OPERATOR_API_PORT", "9001")
    monkeypatch.setenv("AIZK_SERVICE__OPERATOR_API_RELOAD", "true")

    operator_api = ServiceSettings().operator_api

    assert (operator_api.host, operator_api.port, operator_api.reload) == ("127.0.0.1", 9001, True)


def test_operator_api_defaults_off_the_conversion_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """The operator listener's shipped defaults sit apart from the conversion API's port."""
    for name in (
        "AIZK_SERVICE__OPERATOR_API_HOST",
        "AIZK_SERVICE__OPERATOR_API_PORT",
        "AIZK_SERVICE__OPERATOR_API_RELOAD",
        "AIZK_API_PORT",
    ):
        monkeypatch.delenv(name, raising=False)

    service = ServiceSettings()

    assert (service.operator_api.host, service.operator_api.port, service.operator_api.reload) == (
        "0.0.0.0",  # noqa: S104 - asserting the documented all-interfaces default
        8001,
        False,
    )
    assert service.operator_api.port != service.api_port


def test_shipped_defaults_for_mlflow_and_trusted_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unconfigured process gets the documented MLflow and trusted-hosts defaults."""
    for name in (
        "AIZK_MLFLOW__ENABLED",
        "AIZK_MLFLOW__TRACKING_URI",
        "AIZK_MLFLOW__EXPERIMENT_NAME",
        "AIZK_TRUSTED_HOSTS",
    ):
        monkeypatch.delenv(name, raising=False)

    service = ServiceSettings()

    assert (service.mlflow.enabled, service.mlflow.tracking_uri, service.mlflow.experiment_name) == (
        False,
        "",
        "",
    )
    assert service.trusted_hosts == ["localhost", "127.0.0.1"]


def test_api_reload_defaults_to_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIZK_API_RELOAD", raising=False)

    assert ServiceSettings().api_reload is False


def test_service_settings_follows_a_change_to_the_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """A dotenv file read after construction moves a later construction to the new value.

    Settings are read at several points in a process, some of them before and
    some after the environment is populated from a dotenv file.
    """
    env_file = tmp_path / ".env"
    env_file.write_text("AIZK_LOG_LEVEL=DEBUG\n", encoding="utf-8")
    # Registers the name for restoration, so the load below is undone at teardown.
    monkeypatch.delenv("AIZK_LOG_LEVEL", raising=False)

    before = ServiceSettings().log_level
    load_dotenv(dotenv_path=env_file, override=True)
    after = ServiceSettings().log_level

    assert before == "INFO"
    assert after == "DEBUG"
