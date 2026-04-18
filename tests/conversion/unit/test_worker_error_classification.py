"""Subprocess-boundary tests for typed-core pipeline error classification.

Verifies that `FetcherNotRegistered`, `FetcherDepthExceeded`,
`NoConverterForFormat`, and `ChainNotTerminated` are reported as
non-retryable failures at the subprocess boundary — rather than being
swallowed by the `except Exception` catch-all which defaults to
``retryable=True``.
"""

from __future__ import annotations

from pathlib import Path
import queue as queue_module
import tempfile

import pytest

from aizk.conversion.core.errors import (
    ChainNotTerminated,
    FetcherDepthExceeded,
    FetcherNotRegistered,
    NoConverterForFormat,
)
from aizk.conversion.workers import orchestrator


def _build_exc(exc_cls: type[Exception]) -> Exception:
    """Construct each typed error with its real signature."""
    if exc_cls is FetcherNotRegistered:
        return exc_cls("missing")
    if exc_cls is NoConverterForFormat:
        return exc_cls("html", "docling")
    if exc_cls is FetcherDepthExceeded:
        return exc_cls(depth=4, kind="url")
    if exc_cls is ChainNotTerminated:
        return exc_cls("chain not terminated", resolver_name="KaraKeep", missing_kind="arxiv")
    raise AssertionError(f"unexpected exception class: {exc_cls!r}")


def _drain(q: queue_module.Queue) -> list[dict]:
    items: list[dict] = []
    while not q.empty():
        items.append(q.get_nowait())
    return items


# ---------------------------------------------------------------------------
# Class-level retryability + error_code contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc_cls", "expected_code"),
    [
        (FetcherNotRegistered, "fetcher_not_registered"),
        (NoConverterForFormat, "no_converter_for_format"),
        (FetcherDepthExceeded, "fetcher_depth_exceeded"),
        (ChainNotTerminated, "chain_not_terminated"),
    ],
)
def test_typed_core_errors_have_stable_error_code_and_are_non_retryable(
    exc_cls: type[Exception], expected_code: str
) -> None:
    err = _build_exc(exc_cls)
    assert err.retryable is False
    assert err.error_code == expected_code


# ---------------------------------------------------------------------------
# Subprocess-boundary classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc_cls", "expected_code"),
    [
        (FetcherNotRegistered, "fetcher_not_registered"),
        (NoConverterForFormat, "no_converter_for_format"),
        (FetcherDepthExceeded, "fetcher_depth_exceeded"),
        (ChainNotTerminated, "chain_not_terminated"),
    ],
)
def test_subprocess_reports_typed_core_error_as_non_retryable(
    monkeypatch: pytest.MonkeyPatch,
    exc_cls: type[Exception],
    expected_code: str,
) -> None:
    exc = _build_exc(exc_cls)

    def _raise(**_kwargs: object) -> None:
        raise exc

    monkeypatch.setattr(orchestrator, "_convert_job_artifacts", _raise)
    monkeypatch.setattr(orchestrator.os, "setpgrp", lambda: None)

    status_queue: queue_module.Queue = queue_module.Queue()

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        source_ref = workspace / "source_ref.json"
        with pytest.raises(exc_cls):
            orchestrator._process_job_subprocess(
                job_id=1,
                workspace_path=str(workspace),
                source_ref_path=str(source_ref),
                status_queue=status_queue,
            )

    events = _drain(status_queue)
    assert len(events) == 1, events
    event = events[0]
    assert event["event"] == "failed"
    assert event["error_code"] == expected_code
    assert event["retryable"] == "false"
    assert "traceback" in event and event["traceback"]


def test_subprocess_catchall_still_defaults_unknown_errors_to_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: untyped exceptions stay retryable.

    Expanding the typed-error except tuple must not broaden the catch-all.
    """

    class _UnknownError(RuntimeError):
        pass

    def _raise(**_kwargs: object) -> None:
        raise _UnknownError("surprise")

    monkeypatch.setattr(orchestrator, "_convert_job_artifacts", _raise)
    monkeypatch.setattr(orchestrator.os, "setpgrp", lambda: None)

    status_queue: queue_module.Queue = queue_module.Queue()

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        source_ref = workspace / "source_ref.json"
        with pytest.raises(_UnknownError):
            orchestrator._process_job_subprocess(
                job_id=1,
                workspace_path=str(workspace),
                source_ref_path=str(source_ref),
                status_queue=status_queue,
            )

    events = _drain(status_queue)
    assert len(events) == 1, events
    assert events[0]["event"] == "failed"
    assert events[0]["error_code"] == "conversion_failed"
    assert events[0]["retryable"] == "true"
