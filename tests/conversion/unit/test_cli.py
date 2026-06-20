"""Unit tests for the ``aizk-conversion`` CLI dispatch.

These pin the behavior that logging is configured once in ``main`` for *every*
subcommand (not only the worker), with external effects stubbed so the suite
stays hermetic.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from aizk.conversion import cli


@pytest.fixture(autouse=True)
def _stub_process_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize process-global side effects shared by every command."""
    monkeypatch.setattr(cli, "load_process_dotenv_once", lambda: None)
    monkeypatch.setattr(cli, "setproctitle", lambda _title: None)
    monkeypatch.setattr(cli, "ConversionConfig", lambda: SimpleNamespace())


def test_main_configures_logging_before_running_db_init(monkeypatch: pytest.MonkeyPatch) -> None:
    """``main`` configures logging for db-init too — every command shares one logging procedure."""
    calls: list[str] = []
    monkeypatch.setattr(cli, "configure_logging", lambda _config: calls.append("log"))
    monkeypatch.setattr("aizk.conversion.migrations.run_migrations", lambda: calls.append("migrate"))

    assert cli.main(["db-init"]) == 0
    # Logging is configured, and before the command's own work runs.
    assert calls == ["log", "migrate"]
