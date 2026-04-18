"""Unit tests for CLI entrypoints — in particular, startup-validation wiring."""

from __future__ import annotations

import argparse
from unittest.mock import MagicMock, patch

import pytest

from aizk.conversion import cli
from aizk.conversion.utilities.startup import StartupValidationError


@pytest.fixture()
def config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the minimum env vars required by ConversionConfig()."""
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("S3_REGION", "us-east-1")
    monkeypatch.setenv("S3_BUCKET_NAME", "test-bucket")
    monkeypatch.setenv("S3_ENDPOINT_URL", "http://localhost:9000")
    monkeypatch.setenv("AIZK_FETCHER__KARAKEEP__BASE_URL", "http://karakeep.local")
    monkeypatch.setenv("AIZK_FETCHER__KARAKEEP__API_KEY", "test-key")


# ---------------------------------------------------------------------------
# _cmd_serve — startup validation is wired (F1 regression)
# ---------------------------------------------------------------------------


def test_cmd_serve_invokes_validate_startup_with_api_role(config_env) -> None:
    """The serve entry point must gate startup on validate_startup(role='api')."""
    with (
        patch.object(cli, "validate_startup") as mock_validate,
        patch.object(cli, "configure_mlflow_tracing"),
        patch.object(cli, "LitestreamManager") as mock_litestream,
        patch.object(cli, "uvicorn") as mock_uvicorn,
    ):
        mock_litestream.return_value = MagicMock()
        rc = cli._cmd_serve(argparse.Namespace())

    assert rc == 0
    mock_validate.assert_called_once()
    # Second positional arg (or role kwarg) is "api".
    _args, kwargs = mock_validate.call_args
    role = kwargs.get("role") or (_args[1] if len(_args) > 1 else None)
    assert role == "api"
    mock_uvicorn.run.assert_called_once()


def test_cmd_serve_exits_nonzero_on_validation_failure_without_starting_uvicorn(
    config_env,
) -> None:
    """A failing probe must abort before uvicorn starts and return a non-zero code."""
    with (
        patch.object(cli, "validate_startup", side_effect=StartupValidationError("probe failed")),
        patch.object(cli, "configure_mlflow_tracing") as mock_mlflow,
        patch.object(cli, "LitestreamManager") as mock_litestream,
        patch.object(cli, "uvicorn") as mock_uvicorn,
    ):
        rc = cli._cmd_serve(argparse.Namespace())

    assert rc == 1
    # Nothing past the validation gate should have run.
    mock_mlflow.assert_not_called()
    mock_litestream.assert_not_called()
    mock_uvicorn.run.assert_not_called()


def test_cmd_serve_validate_startup_runs_before_uvicorn(config_env) -> None:
    """validate_startup must complete before uvicorn.run is entered."""
    call_order: list[str] = []

    def _record_validate(*_a, **_kw) -> None:
        call_order.append("validate")

    def _record_uvicorn(*_a, **_kw) -> None:
        call_order.append("uvicorn")

    with (
        patch.object(cli, "validate_startup", side_effect=_record_validate),
        patch.object(cli, "configure_mlflow_tracing"),
        patch.object(cli, "LitestreamManager") as mock_litestream,
        patch.object(cli, "uvicorn") as mock_uvicorn,
    ):
        mock_litestream.return_value = MagicMock()
        mock_uvicorn.run.side_effect = _record_uvicorn
        cli._cmd_serve(argparse.Namespace())

    assert call_order == ["validate", "uvicorn"]
