"""Unit tests for the ``aizk-graph`` CLI dispatch.

The CLI is a thin argument parser over two commands; these tests pin the command
surface and the wiring of each command to its action, with every external effect
(uvicorn, migrations, the worker loop, config construction) stubbed so the suite
stays hermetic and never reads ``.env`` or opens a socket.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from aizk.graph import cli
from aizk.graph.config import ContextualizationConfig


@pytest.fixture(autouse=True)
def _stub_process_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize process-global side effects shared by every command."""
    monkeypatch.setattr(cli, "load_process_dotenv_once", lambda: None)
    monkeypatch.setattr(cli, "setproctitle", lambda _title: None)
    monkeypatch.setattr(cli, "configure_logging", lambda _config: None)
    monkeypatch.setattr(cli, "ConversionConfig", lambda: SimpleNamespace())


def test_operator_api_port_defaults_off_the_conversion_port() -> None:
    """The operator API has its own listener default, distinct from conversion's 8000."""
    config = ContextualizationConfig()
    assert config.operator_api_port == 8001
    assert config.operator_api_host == "0.0.0.0"  # noqa: S104 - asserting the documented all-interfaces default
    assert config.operator_api_reload is False


def test_serve_runs_the_operator_app_factory_on_the_operator_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """``serve`` runs the graph operator app via uvicorn's factory at the operator-API listener."""
    monkeypatch.setattr(
        cli,
        "ContextualizationConfig",
        lambda: SimpleNamespace(operator_api_host="127.0.0.1", operator_api_port=8001, operator_api_reload=False),
    )
    calls: dict[str, object] = {}

    def fake_run(app: str, **kwargs: object) -> None:
        calls["app"] = app
        calls.update(kwargs)

    monkeypatch.setattr(cli.uvicorn, "run", fake_run)

    assert cli.main(["serve"]) == 0
    assert calls["app"] == "aizk.graph.api.main:create_app"
    assert calls["factory"] is True
    assert (calls["host"], calls["port"]) == ("127.0.0.1", 8001)


def test_serve_does_not_run_migrations(monkeypatch: pytest.MonkeyPatch) -> None:
    """``serve`` is not a migrator (a worker or ``db-init`` is); it must not migrate on start."""
    monkeypatch.setattr(
        cli,
        "ContextualizationConfig",
        lambda: SimpleNamespace(operator_api_host="127.0.0.1", operator_api_port=8001, operator_api_reload=False),
    )
    monkeypatch.setattr(cli.uvicorn, "run", lambda *_a, **_k: None)

    def fail_migrate() -> None:
        raise AssertionError("serve must not run migrations")

    monkeypatch.setattr(cli, "run_migrations", fail_migrate)
    assert cli.main(["serve"]) == 0


def test_worker_runs_migrations_then_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """``worker`` migrates first, then runs the worker loop and returns its exit code."""
    order: list[str] = []
    monkeypatch.setattr(cli, "ContextualizationConfig", lambda: SimpleNamespace())
    monkeypatch.setattr(cli, "run_migrations", lambda: order.append("migrate"))
    monkeypatch.setattr(cli, "run_graph_worker", lambda _c, _cc: order.append("loop") or 0)

    assert cli.main(["worker"]) == 0
    assert order == ["migrate", "loop"]


def test_worker_maps_startup_validation_error_to_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed startup gate (model endpoint unset) exits non-zero rather than crashing."""
    monkeypatch.setattr(cli, "ContextualizationConfig", lambda: SimpleNamespace())
    monkeypatch.setattr(cli, "run_migrations", lambda: None)

    def raise_startup(_config: object, _ctx: object) -> int:
        raise cli.StartupValidationError("model endpoint not configured")

    monkeypatch.setattr(cli, "run_graph_worker", raise_startup)
    assert cli.main(["worker"]) == 1


def test_unknown_or_missing_command_is_rejected() -> None:
    """A subcommand is required, and an unknown one is rejected (argparse exits)."""
    with pytest.raises(SystemExit):
        cli.main([])
    with pytest.raises(SystemExit):
        cli.main(["nope"])


def test_db_init_is_not_a_graph_command() -> None:
    """Schema init is conversion's command over the shared Alembic tree, not graph's."""
    with pytest.raises(SystemExit):
        cli.main(["db-init"])
