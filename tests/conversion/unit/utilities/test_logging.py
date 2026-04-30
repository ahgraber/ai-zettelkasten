"""Tests for the structured logging configuration.

Regression tests for the H1 audit-log gap: every WARNING emitted by the
egress code (egress.py / egress_fetch.py / html_prefetch.py / paths.py) attaches
``extra={"url": ..., "host": ..., "ip": ..., "error_class": ...}`` so an
operator can reconstruct *which* destination was rejected and *why* without
re-running the request. The persisted ``error_message`` is intentionally
sanitized at the orchestrator (``_EGRESS_POLICY_ERROR_CODES`` filter); the
WARNING log is the only forensic record. The formatter MUST therefore pass
through arbitrary ``extra`` keys, and the worker subprocess MUST configure
logging before any egress code runs.
"""

from __future__ import annotations

import json
import logging

import pytest

from aizk.conversion.utilities.logging import (
    ContextFilter,
    JsonFormatter,
    configure_logging,
)


def _make_record(
    *,
    name: str = "aizk.test",
    level: int = logging.WARNING,
    msg: str = "Egress denied: resolved address in deny set",
    extra: dict[str, object] | None = None,
) -> logging.LogRecord:
    """Build a LogRecord shaped like the egress code's WARNING calls."""
    record = logging.LogRecord(
        name=name,
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    # Apply the same ContextFilter the production logger uses so the static
    # context keys exist on the record.
    ContextFilter().filter(record)
    if extra:
        for key, value in extra.items():
            setattr(record, key, value)
    return record


# ---------------------------------------------------------------------------
# JsonFormatter — must pass through arbitrary `extra` keys
# ---------------------------------------------------------------------------


def test_json_formatter_passes_through_arbitrary_extra_keys() -> None:
    """A WARNING with ``extra={"url": ..., "host": ..., "ip": ...}`` must serialize all keys.

    Regression for H1: previously the formatter only emitted a static four-field
    set (``aizk_uuid``, ``job_id``, ``karakeep_id``, ``status``) and silently
    dropped every egress field. With persisted ``error_message`` sanitized,
    operators had no recovery path to the rejected destination.
    """
    record = _make_record(
        extra={
            "url": "http://internal.corp.example/path",
            "host": "internal.corp.example",
            "ip": "10.0.0.5",
            "error_class": "DenyListDestination",
        },
    )

    payload = json.loads(JsonFormatter().format(record))

    # Static fields preserved.
    assert payload["level"] == "WARNING"
    assert payload["message"] == "Egress denied: resolved address in deny set"
    # Arbitrary extras flow through.
    assert payload["url"] == "http://internal.corp.example/path"
    assert payload["host"] == "internal.corp.example"
    assert payload["ip"] == "10.0.0.5"
    assert payload["error_class"] == "DenyListDestination"


def test_json_formatter_preserves_static_context_fields_when_set() -> None:
    """When the LoggerAdapter populates aizk_uuid / job_id, those still appear."""
    record = _make_record(extra={"aizk_uuid": "uu-1", "job_id": 42})
    payload = json.loads(JsonFormatter().format(record))
    assert payload["aizk_uuid"] == "uu-1"
    assert payload["job_id"] == 42


def test_json_formatter_does_not_leak_internal_logrecord_attrs() -> None:
    """Standard LogRecord attributes (``args``, ``msg``, ``pathname``, ``filename``,
    ``module``, ``exc_info``, ``stack_info``, ``lineno``, ``funcName``, ``created``,
    etc.) must NOT appear as top-level fields — they are noise and may carry
    implementation details."""
    record = _make_record(extra={"url": "http://example.com"})
    payload = json.loads(JsonFormatter().format(record))
    # Sentinel: random standard attrs that should never appear.
    for noisy in ("args", "msg", "pathname", "filename", "module", "exc_info"):
        assert noisy not in payload, f"{noisy} leaked into JSON output"


def test_json_formatter_handles_non_json_serialisable_extras() -> None:
    """``extra`` may carry exception classes or paths; the formatter must not crash."""

    class _Custom:
        def __repr__(self) -> str:
            return "<Custom thingy>"

    record = _make_record(extra={"err": _Custom()})
    text = JsonFormatter().format(record)
    payload = json.loads(text)
    # ``default=str`` style fallback expected.
    assert "err" in payload
    assert "Custom" in payload["err"]


# ---------------------------------------------------------------------------
# Plain-text formatter — must pass through arbitrary `extra` keys
# ---------------------------------------------------------------------------


def test_plain_text_format_includes_arbitrary_extra_keys(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When configured for plain text, an emitted WARNING with ``extra={url, host, ip}``
    must surface those fields in the rendered log line.

    Without this, a non-JSON deployment loses the egress audit trail entirely.
    """
    from aizk.conversion.utilities.config import ConversionConfig

    monkeypatch.setenv("AIZK_LOG_FORMAT", "text")
    monkeypatch.setenv("AIZK_LOG_LEVEL", "WARNING")

    # Re-configure the root logger using the production code path.
    configure_logging(ConversionConfig(_env_file=None))

    logger = logging.getLogger("aizk.test_plain")
    # Capture the rendered output the handler produces.
    handler = logging.getLogger().handlers[0]
    rendered: list[str] = []
    original_emit = handler.emit

    def _capture_emit(record: logging.LogRecord) -> None:
        rendered.append(handler.format(record))
        original_emit(record)

    monkeypatch.setattr(handler, "emit", _capture_emit)

    logger.warning(
        "Egress denied: resolved address in deny set",
        extra={
            "url": "http://internal.corp.example/admin",
            "host": "internal.corp.example",
            "ip": "10.0.0.5",
        },
    )

    assert rendered, "handler.emit was not called"
    last = rendered[-1]
    assert "internal.corp.example" in last
    assert "10.0.0.5" in last
    assert "internal.corp.example/admin" in last


# ---------------------------------------------------------------------------
# End-to-end smoke: an egress denial produces a log carrying the destination
# ---------------------------------------------------------------------------


def test_egress_denial_log_line_carries_rejected_destination(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``assert_egress_allowed`` rejects a private IP, the rendered log
    record (after the JSON formatter runs) must contain the rejected host and IP.

    This is the integration-level form of H1: the design promises that the
    rejected destination SHALL be in WARNING logs even though it MUST NOT be in
    persisted ``error_message``. Without formatter passthrough, this contract
    silently fails."""
    import socket

    from aizk.conversion.core.errors import DenyListDestination
    from aizk.conversion.utilities.egress import assert_egress_allowed

    private_ip = "10.0.0.5"

    def _deny_set_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (private_ip, port))]

    monkeypatch.setattr(socket, "getaddrinfo", _deny_set_getaddrinfo)

    with caplog.at_level(logging.WARNING, logger="aizk.conversion.utilities.egress"):
        with pytest.raises(DenyListDestination):
            assert_egress_allowed("http://private.corp.example/page")

    # Find the deny WARNING.
    deny_record = next(
        (r for r in caplog.records if r.levelno == logging.WARNING and "deny" in r.getMessage().lower()),
        None,
    )
    assert deny_record is not None

    # Render through the production JsonFormatter — what an operator would grep.
    rendered = JsonFormatter().format(deny_record)
    payload = json.loads(rendered)
    assert payload["host"] == "private.corp.example"
    assert payload["ip"] == private_ip


# ---------------------------------------------------------------------------
# Worker subprocess MUST call configure_logging
# ---------------------------------------------------------------------------


def test_cmd_worker_configures_logging_before_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """``cli._cmd_worker`` must invoke ``configure_logging`` before starting the worker loop.

    Without this, every WARNING raised inside the worker process emits via
    Python's ``lastResort`` (stderr-only, default format) — losing the JSON
    envelope and the egress ``extra`` keys. The H1 audit-log contract is
    silently broken.
    """
    import argparse

    from aizk.conversion import cli as cli_module

    configure_calls: list[object] = []

    def _fake_configure(config: object) -> None:
        configure_calls.append(config)

    # Stub out everything else _cmd_worker does so the test isolates the
    # configure_logging call.
    monkeypatch.setattr(cli_module, "configure_logging", _fake_configure)
    monkeypatch.setattr(cli_module, "log_feature_summary", lambda *_a, **_kw: None)
    monkeypatch.setattr(cli_module, "validate_startup", lambda *_a, **_kw: None)
    monkeypatch.setattr(cli_module, "configure_mlflow_tracing", lambda **_kw: None)
    monkeypatch.setattr(cli_module, "setproctitle", lambda _t: None)

    # ``run_migrations`` is imported lazily inside ``_cmd_worker``; patch the
    # source module so the lazy import resolves to a no-op.
    import aizk.conversion.migrations as migrations_module

    monkeypatch.setattr(migrations_module, "run_migrations", lambda: None)

    class _FakeLitestreamManager:
        def __init__(self, *_a, **_kw):
            pass

        def start(self):
            return None

    monkeypatch.setattr(cli_module, "LitestreamManager", _FakeLitestreamManager)

    fake_run_worker_calls: list[object] = []

    # ``run_worker`` is imported lazily inside _cmd_worker; intercept it.
    import aizk.conversion.workers.loop as worker_loop

    def _fake_run_worker(config: object) -> int:
        fake_run_worker_calls.append(config)
        return 0

    monkeypatch.setattr(worker_loop, "run_worker", _fake_run_worker)

    # Run the worker command.
    rc = cli_module._cmd_worker(argparse.Namespace())

    assert rc == 0
    assert len(configure_calls) == 1, "configure_logging must be called exactly once"
    # configure_logging must run BEFORE run_worker dispatches.
    assert len(fake_run_worker_calls) == 1
