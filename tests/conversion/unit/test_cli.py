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
    monkeypatch.setattr("aizk.db.migrations.run_migrations", lambda: calls.append("migrate"))

    assert cli.main(["db-init"]) == 0
    # Logging is configured, and before the command's own work runs.
    assert calls == ["log", "migrate"]


def _stub_backfill_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the config and client construction the backfill command performs."""
    monkeypatch.setattr(cli, "configure_logging", lambda _config: None)
    monkeypatch.setattr(cli, "ConversionConfig", lambda: SimpleNamespace(api_host="127.0.0.1", api_port=8000))
    monkeypatch.setattr(cli, "KarakeepFetcherConfig", lambda: SimpleNamespace(base_url="http://kk", api_key="k"))
    monkeypatch.setattr(cli, "KarakeepClient", lambda **_kwargs: SimpleNamespace())


def test_backfill_without_confirmation_is_refused_before_any_submission(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A conversion backfill is corpus-wide reprocessing, so it does not run without ``--yes``.

    A converter config change gives every bookmark a fresh idempotency key, so an
    unconfirmed sweep would re-convert the whole corpus. Nothing is submitted and
    the refusal names the flags that resolve it.
    """
    _stub_backfill_clients(monkeypatch)

    async def fail_if_called(**_kwargs: object) -> object:
        raise AssertionError("the backfill must not run without confirmation")

    monkeypatch.setattr(cli, "run_conversion_backfill", fail_if_called)

    assert cli.main(["backfill"]) == 1
    captured = capsys.readouterr()
    assert "large downstream blast radius" in captured.err
    assert "--yes" in captured.err
    assert "Traceback" not in captured.err


def test_backfill_dry_run_needs_no_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dry run submits nothing, so there is no blast radius to sign off on."""
    _stub_backfill_clients(monkeypatch)
    captured: dict[str, object] = {}

    async def fake_run(**kwargs: object) -> cli.ConversionBackfillResult:
        captured.update(kwargs)
        return cli.ConversionBackfillResult(submitted=2, existing=0, failed=0)

    monkeypatch.setattr(cli, "run_conversion_backfill", fake_run)

    assert cli.main(["backfill", "--dry-run"]) == 0
    assert captured["dry_run"] is True


def test_backfill_limit_does_not_exempt_a_run_from_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bounded sweep still submits real work, so ``--limit`` is not a substitute for ``--yes``."""
    _stub_backfill_clients(monkeypatch)

    async def fail_if_called(**_kwargs: object) -> object:
        raise AssertionError("the backfill must not run without confirmation")

    monkeypatch.setattr(cli, "run_conversion_backfill", fail_if_called)

    assert cli.main(["backfill", "--limit", "10"]) == 1


def test_backfill_forwards_parsed_options_and_reports_counts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``backfill`` passes its parsed options through and reports the run's counts."""
    _stub_backfill_clients(monkeypatch)
    captured: dict[str, object] = {}

    async def fake_run(**kwargs: object) -> cli.ConversionBackfillResult:
        captured.update(kwargs)
        return cli.ConversionBackfillResult(submitted=4, existing=2, failed=0)

    monkeypatch.setattr(cli, "run_conversion_backfill", fake_run)

    assert cli.main(["backfill", "--limit", "10", "--page-size", "50", "--yes"]) == 0
    assert captured["limit"] == 10
    assert captured["page_size"] == 50
    assert captured["dry_run"] is False
    out = capsys.readouterr().out
    assert "4 submitted" in out
    assert "2 already queued" in out
    assert "aizk-conversion worker" in out, "the operator is told what drains the queue"


def test_backfill_unreachable_api_exits_nonzero_naming_the_command_to_start_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A down API is a clean refusal on stderr naming how to start it, not a traceback."""
    _stub_backfill_clients(monkeypatch)

    async def fake_run(**_kwargs: object) -> object:
        raise cli.ConversionApiUnreachableError(
            "conversion API is not reachable at http://127.0.0.1:8000 — start it with `aizk-conversion serve`."
        )

    monkeypatch.setattr(cli, "run_conversion_backfill", fake_run)

    assert cli.main(["backfill", "--yes"]) == 1
    captured = capsys.readouterr()
    assert "not reachable" in captured.err
    assert "aizk-conversion serve" in captured.err
    assert "Traceback" not in captured.err


def test_backfill_reports_an_unconfigured_karakeep_client_as_a_usage_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Missing KaraKeep credentials are a configuration error, reported without a traceback."""
    monkeypatch.setattr(cli, "configure_logging", lambda _config: None)
    monkeypatch.setattr(cli, "ConversionConfig", lambda: SimpleNamespace(api_host="127.0.0.1", api_port=8000))
    monkeypatch.setattr(cli, "KarakeepFetcherConfig", lambda: SimpleNamespace(base_url=None, api_key=None))

    def raise_unconfigured(**_kwargs: object) -> object:
        raise ValueError("API key must be provided or set in KARAKEEP_API_KEY environment variable")

    monkeypatch.setattr(cli, "KarakeepClient", raise_unconfigured)

    assert cli.main(["backfill", "--yes"]) == 1
    captured = capsys.readouterr()
    assert "API key must be provided" in captured.err
    assert "Traceback" not in captured.err


def test_backfill_reports_nonzero_when_a_bookmark_failed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A run with failures exits non-zero and reports the count, so a scripted backfill does not read as clean."""
    _stub_backfill_clients(monkeypatch)

    async def fake_run(**_kwargs: object) -> cli.ConversionBackfillResult:
        return cli.ConversionBackfillResult(submitted=1, existing=0, failed=2)

    monkeypatch.setattr(cli, "run_conversion_backfill", fake_run)

    assert cli.main(["backfill", "--yes"]) == 1
    assert "2 failed" in capsys.readouterr().out
