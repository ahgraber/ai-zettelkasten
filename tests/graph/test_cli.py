"""Unit tests for the ``aizk-graph`` CLI dispatch.

The CLI is a thin argument parser over the graph stage's commands; these tests
pin the command surface and the wiring of each command to its action, with every
external effect (uvicorn, migrations, the worker loop, huggingface_hub, backfill
runs, config construction) stubbed so the suite stays hermetic and never reads
``.env``, opens a socket, or touches the network.
"""

from __future__ import annotations

import sys
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


def test_extraction_worker_runs_migrations_then_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """``extraction-worker`` migrates first, then runs the worker loop and returns its exit code."""
    order: list[str] = []
    monkeypatch.setattr(cli, "ExtractionConfig", lambda: SimpleNamespace())
    monkeypatch.setattr(cli, "run_migrations", lambda: order.append("migrate"))
    monkeypatch.setattr(cli, "run_extraction_worker", lambda _c: order.append("loop") or 0)

    assert cli.main(["extraction-worker"]) == 0
    assert order == ["migrate", "loop"]


def test_extraction_worker_maps_import_error_to_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed startup gate (extractor's pinned dependency/model missing) exits non-zero rather than crashing."""
    monkeypatch.setattr(cli, "ExtractionConfig", lambda: SimpleNamespace())
    monkeypatch.setattr(cli, "run_migrations", lambda: None)

    def raise_import_error(_config: object) -> int:
        raise ImportError("GLiNER2 weights are not present")

    monkeypatch.setattr(cli, "run_extraction_worker", raise_import_error)
    assert cli.main(["extraction-worker"]) == 1


def test_fetch_gliner2_weights_downloads_pinned_revision_to_configured_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """``fetch-gliner2-weights`` downloads the configured repo/revision into the configured local directory."""
    monkeypatch.setattr(
        cli,
        "NerConfig",
        lambda: SimpleNamespace(gliner2_model_dir="data/models/gliner2-base-v1", gliner2_revision="deadbeef"),
    )
    calls: dict[str, object] = {}

    def fake_snapshot_download(**kwargs: object) -> str:
        calls.update(kwargs)
        return kwargs["local_dir"]

    monkeypatch.setattr("huggingface_hub.snapshot_download", fake_snapshot_download)

    assert cli.main(["fetch-gliner2-weights"]) == 0
    assert calls == {
        "repo_id": cli.GLINER2_REPO_ID,
        "revision": "deadbeef",
        "local_dir": "data/models/gliner2-base-v1",
    }


def test_fetch_gliner2_weights_missing_huggingface_hub_returns_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing ``huggingface_hub`` install exits non-zero rather than crashing."""
    monkeypatch.setattr(cli, "NerConfig", lambda: SimpleNamespace(gliner2_model_dir="x", gliner2_revision="y"))
    monkeypatch.setitem(sys.modules, "huggingface_hub", None)

    assert cli.main(["fetch-gliner2-weights"]) == 1


class _FakeSessionCtx:
    """A no-op ``Session``-shaped context manager standing in for ``sqlmodel.Session`` in dispatch tests.

    ``_cmd_extract_dataset`` opens a session only to hand it to
    ``compute_dataset_statistics``, which is itself stubbed in these tests, so
    this context manager never touches a real engine or database.
    """

    def __init__(self, _engine: object) -> None:
        """Discard the engine argument; nothing is opened."""

    def __enter__(self) -> SimpleNamespace:
        """Return a placeholder session object."""
        return SimpleNamespace()

    def __exit__(self, *_exc_info: object) -> bool:
        """No cleanup is needed."""
        return False


def test_extract_dataset_runs_migrations_extracts_and_prints_statistics(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``extract-dataset`` migrates, builds the extractor, runs the dataset extraction, and prints stats JSON."""
    order: list[str] = []
    monkeypatch.setattr(cli, "ExtractionConfig", lambda: SimpleNamespace(input_policy="contextualized"))
    monkeypatch.setattr(cli, "run_migrations", lambda: order.append("migrate"))
    monkeypatch.setattr(cli, "build_extractor", lambda _config: SimpleNamespace(extractor_version="stub/v1"))
    monkeypatch.setattr(cli, "DatabaseConfig", lambda: SimpleNamespace(database_url="sqlite:///:memory:"))
    monkeypatch.setattr(cli, "get_engine", lambda _url: "fake-engine")
    monkeypatch.setattr(cli, "Session", _FakeSessionCtx)

    captured_kwargs: dict[str, object] = {}

    def fake_run_dataset_extraction(_engine: object, **kwargs: object) -> list[SimpleNamespace]:
        order.append("extract")
        captured_kwargs.update(kwargs)
        return [SimpleNamespace(source_id="s1", mention_count=3)]

    monkeypatch.setattr(cli, "run_dataset_extraction", fake_run_dataset_extraction)
    monkeypatch.setattr(
        cli, "compute_dataset_statistics", lambda _session: SimpleNamespace(model_dump_json=lambda **_k: '{"ok":true}')
    )

    exit_code = cli.main(["extract-dataset", "--source-id", "s1", "--source-id", "s2", "--limit", "5", "--yes"])

    assert exit_code == 0
    assert order == ["migrate", "extract"]
    assert captured_kwargs["source_ids"] == ["s1", "s2"]
    assert captured_kwargs["limit"] == 5
    assert captured_kwargs["confirmed"] is True
    assert captured_kwargs["input_policy"] == "contextualized"
    assert '"ok":true' in capsys.readouterr().out


def test_extract_dataset_maps_extractor_import_error_to_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed extractor construction (pinned dependency/model missing) exits non-zero rather than crashing."""
    monkeypatch.setattr(cli, "ExtractionConfig", lambda: SimpleNamespace(input_policy="raw"))
    monkeypatch.setattr(cli, "run_migrations", lambda: None)

    def raise_import_error(_config: object) -> object:
        raise ImportError("GLiNER2 weights are not present")

    monkeypatch.setattr(cli, "build_extractor", raise_import_error)

    assert cli.main(["extract-dataset"]) == 1


def test_extract_dataset_confirmation_gate_refusal_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A corpus-scan target selection (no ``--source-id``) without ``--yes`` is refused; exits non-zero.

    Mirrors ``tests/graph/test_extraction_workunit.py``'s coverage of
    ``enqueue_extraction_backfill``'s confirmation-gate refusal: the same
    :class:`~aizk.pipeline.invalidation.ReprocessingConfirmationError` gate,
    surfaced through the CLI's dispatch layer rather than raised past it. Every
    gated command reports the refusal the same way, naming the flags that
    resolve it rather than emitting a traceback.
    """
    monkeypatch.setattr(cli, "ExtractionConfig", lambda: SimpleNamespace(input_policy="raw"))
    monkeypatch.setattr(cli, "run_migrations", lambda: None)
    monkeypatch.setattr(cli, "build_extractor", lambda _config: SimpleNamespace(extractor_version="stub/v1"))
    monkeypatch.setattr(cli, "DatabaseConfig", lambda: SimpleNamespace(database_url="sqlite:///:memory:"))
    monkeypatch.setattr(cli, "get_engine", lambda _url: "fake-engine")

    def raise_confirmation_error(_engine: object, **_kwargs: object) -> list[object]:
        raise cli.ReprocessingConfirmationError(
            "corpus-scanning extraction dataset run has a large downstream blast radius and will not run "
            "until it is explicitly confirmed; re-invoke with confirmation to approve."
        )

    monkeypatch.setattr(cli, "run_dataset_extraction", raise_confirmation_error)

    assert cli.main(["extract-dataset"]) == 1
    captured = capsys.readouterr()
    assert "large downstream blast radius" in captured.err
    assert "--yes" in captured.err
    assert "--source-id" in captured.err
    assert "Traceback" not in captured.err


def _stub_backfill_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub migrations and engine construction shared by both backfill commands."""
    monkeypatch.setattr(cli, "run_migrations", lambda: None)
    monkeypatch.setattr(cli, "DatabaseConfig", lambda: SimpleNamespace(database_url="sqlite:///:memory:"))
    monkeypatch.setattr(cli, "get_engine", lambda _url: "fake-engine")


def test_backfill_runs_migrations_then_enqueues_with_parsed_targets(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``backfill`` migrates, then passes its parsed target selection to the backfill run."""
    order: list[str] = []
    monkeypatch.setattr(cli, "run_migrations", lambda: order.append("migrate"))
    monkeypatch.setattr(cli, "DatabaseConfig", lambda: SimpleNamespace(database_url="sqlite:///:memory:"))
    monkeypatch.setattr(cli, "get_engine", lambda _url: "fake-engine")
    captured: dict[str, object] = {}

    def fake_backfill(_engine: object, **kwargs: object) -> cli.BackfillResult:
        order.append("backfill")
        captured.update(kwargs)
        return cli.BackfillResult(targeted=3, enqueued=2, reused=1)

    monkeypatch.setattr(cli, "run_contextualization_backfill", fake_backfill)

    exit_code = cli.main(["backfill", "--output-id", "7", "--output-id", "9", "--yes"])

    assert exit_code == 0
    assert order == ["migrate", "backfill"]
    assert captured["output_ids"] == [7, 9]
    assert captured["confirmed"] is True
    assert captured["dry_run"] is False


def test_backfill_corpus_scan_passes_limit_and_no_explicit_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without ``--output-id`` the run receives ``None``, marking an implicit corpus scan."""
    _stub_backfill_engine(monkeypatch)
    captured: dict[str, object] = {}

    def fake_backfill(_engine: object, **kwargs: object) -> cli.BackfillResult:
        captured.update(kwargs)
        return cli.BackfillResult(targeted=0, enqueued=0, reused=0)

    monkeypatch.setattr(cli, "run_contextualization_backfill", fake_backfill)

    assert cli.main(["backfill", "--limit", "25", "--yes"]) == 0
    assert captured["output_ids"] is None
    assert captured["limit"] == 25


def test_backfill_reports_counts_and_points_at_the_worker(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The run reports its counts and names the worker that drains what it enqueued."""
    _stub_backfill_engine(monkeypatch)
    monkeypatch.setattr(
        cli,
        "run_contextualization_backfill",
        lambda _e, **_k: cli.BackfillResult(targeted=3, enqueued=2, reused=1),
    )

    assert cli.main(["backfill", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "3 targeted" in out
    assert "2 enqueued" in out
    assert "1 reused" in out
    assert "aizk-graph worker" in out, "the operator is told what drains the queue"


def test_backfill_dry_run_reports_that_nothing_was_written(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dry run forwards the flag and its report does not claim work was queued."""
    _stub_backfill_engine(monkeypatch)
    captured: dict[str, object] = {}

    def fake_backfill(_engine: object, **kwargs: object) -> cli.BackfillResult:
        captured.update(kwargs)
        return cli.BackfillResult(targeted=3, enqueued=2, reused=1)

    monkeypatch.setattr(cli, "run_contextualization_backfill", fake_backfill)

    assert cli.main(["backfill", "--dry-run", "--yes"]) == 0
    assert captured["dry_run"] is True
    assert "dry run" in capsys.readouterr().out


@pytest.mark.parametrize("subcommand", ["backfill", "extract-dataset"])
@pytest.mark.parametrize("limit", ["-1", "0"])
def test_a_limit_that_does_not_bound_is_rejected_at_parse_time(subcommand: str, limit: str) -> None:
    """``--limit -1`` reads as a restriction but SQLite treats it as none, so it never parses."""
    with pytest.raises(SystemExit):
        cli.main([subcommand, "--limit", limit, "--yes"])


def test_backfill_gate_refusal_names_the_flags_that_resolve_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A refused corpus scan exits non-zero with a message naming ``--yes`` and ``--output-id``."""
    _stub_backfill_engine(monkeypatch)

    def raise_confirmation_error(_engine: object, **_kwargs: object) -> object:
        raise cli.ReprocessingConfirmationError("gated")

    monkeypatch.setattr(cli, "run_contextualization_backfill", raise_confirmation_error)

    assert cli.main(["backfill"]) == 1
    captured = capsys.readouterr()
    assert "large downstream blast radius" in captured.err
    assert "--yes" in captured.err
    assert "--output-id" in captured.err
    assert "Traceback" not in captured.err, "a missing flag is a usage refusal, not a crash"


def test_backfill_reports_an_unknown_output_id_as_a_usage_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A named output that does not exist is bad operator input, reported without a traceback."""
    _stub_backfill_engine(monkeypatch)

    def raise_unknown_output(_engine: object, **_kwargs: object) -> object:
        raise ValueError("conversion output 999999 not found")

    monkeypatch.setattr(cli, "run_contextualization_backfill", raise_unknown_output)

    assert cli.main(["backfill", "--output-id", "999999"]) == 1
    captured = capsys.readouterr()
    assert "conversion output 999999 not found" in captured.err
    assert "Traceback" not in captured.err


def test_extraction_backfill_passes_parsed_source_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """``extraction-backfill`` parses ``--source-id`` into UUIDs before handing them to the run."""
    from uuid import UUID

    _stub_backfill_engine(monkeypatch)
    captured: dict[str, object] = {}

    def fake_backfill(_engine: object, **kwargs: object) -> cli.BackfillResult:
        captured.update(kwargs)
        return cli.BackfillResult(targeted=1, enqueued=1, reused=0)

    monkeypatch.setattr(cli, "run_extraction_backfill", fake_backfill)

    exit_code = cli.main(["extraction-backfill", "--source-id", "11111111-1111-1111-1111-111111111111"])

    assert exit_code == 0
    assert captured["source_ids"] == [UUID("11111111-1111-1111-1111-111111111111")]
    assert captured["confirmed"] is False


def test_extraction_backfill_rejects_a_malformed_source_id() -> None:
    """A ``--source-id`` that is not a UUID is rejected at parse time, before any work runs."""
    with pytest.raises(SystemExit):
        cli.main(["extraction-backfill", "--source-id", "not-a-uuid"])


def test_extraction_backfill_gate_refusal_names_the_flags_that_resolve_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A refused corpus scan exits non-zero with a message naming ``--yes`` and ``--source-id``."""
    _stub_backfill_engine(monkeypatch)

    def raise_confirmation_error(_engine: object, **_kwargs: object) -> object:
        raise cli.ReprocessingConfirmationError("gated")

    monkeypatch.setattr(cli, "run_extraction_backfill", raise_confirmation_error)

    assert cli.main(["extraction-backfill"]) == 1
    captured = capsys.readouterr()
    assert "large downstream blast radius" in captured.err
    assert "--yes" in captured.err
    assert "--source-id" in captured.err


def test_extraction_backfill_points_at_its_own_worker(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The extraction report names the extraction worker, not the contextualization one."""
    _stub_backfill_engine(monkeypatch)
    monkeypatch.setattr(
        cli, "run_extraction_backfill", lambda _e, **_k: cli.BackfillResult(targeted=1, enqueued=1, reused=0)
    )

    assert cli.main(["extraction-backfill", "--yes"]) == 0
    assert "aizk-graph extraction-worker" in capsys.readouterr().out


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
