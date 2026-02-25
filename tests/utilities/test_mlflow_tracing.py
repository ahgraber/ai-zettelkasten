from __future__ import annotations

from contextlib import contextmanager

import pytest

from aizk.utilities import mlflow_tracing


class _FakeSpan:
    def __init__(self) -> None:
        self.attributes: dict[str, object] = {}

    def set_attributes(self, attrs: dict[str, object]) -> None:
        self.attributes.update(attrs)


class _FakeMlflow:
    def __init__(self, span: _FakeSpan) -> None:
        self._span = span
        self.calls: list[tuple[str, object]] = []

    def set_tracking_uri(self, value: str) -> None:
        self.calls.append(("set_tracking_uri", value))

    def set_experiment(self, value: str) -> None:
        self.calls.append(("set_experiment", value))

    @contextmanager
    def start_span(self, name: str, span_type: str, attributes: dict[str, object]):
        self.calls.append(("start_span", (name, span_type, attributes)))
        self._span.set_attributes(attributes)
        yield self._span


def test_sanitize_trace_attributes() -> None:
    attributes = {
        "model": "text-embedding-3-small",
        "api_key": "secret-value",
        "Authorization": "Bearer secret",
        "prompt": "raw text",
        "request_count": 3,
    }

    sanitized = mlflow_tracing.sanitize_trace_attributes(attributes)

    assert sanitized == {
        "model": "text-embedding-3-small",
        "request_count": 3,
    }


def test_trace_model_call_noop_when_disabled(monkeypatch) -> None:
    mlflow_tracing.configure_mlflow_tracing(enabled=False)
    monkeypatch.delenv("MLFLOW_TRACING_ENABLED", raising=False)

    with mlflow_tracing.trace_model_call(name="embedding.batch", span_type="EMBEDDING", attributes={"model": "m"}):
        executed = True

    assert executed is True


def test_trace_model_call_sets_status_and_latency(monkeypatch) -> None:
    span = _FakeSpan()
    fake_mlflow = _FakeMlflow(span)

    mlflow_tracing.configure_mlflow_tracing(
        enabled=True,
        tracking_uri="http://mlflow:5000",
        experiment_name="aizk-conversion",
    )
    monkeypatch.setattr(mlflow_tracing, "_load_mlflow", lambda: fake_mlflow)

    with mlflow_tracing.trace_model_call(
        name="llm.chat.completions.batch",
        span_type="CHAT_MODEL",
        attributes={"model": "gpt-test"},
    ):
        pass

    assert ("set_tracking_uri", "http://mlflow:5000") in fake_mlflow.calls
    assert ("set_experiment", "aizk-conversion") in fake_mlflow.calls
    assert span.attributes["model"] == "gpt-test"
    assert span.attributes["status"] == "ok"
    assert "latency_ms" in span.attributes


def test_trace_model_call_is_non_disruptive_on_span_creation_failure(monkeypatch) -> None:
    class _FailingMlflow:
        def start_span(self, **_kwargs):
            raise RuntimeError("cannot start span")

    mlflow_tracing.configure_mlflow_tracing(enabled=True)
    monkeypatch.setattr(mlflow_tracing, "_load_mlflow", lambda: _FailingMlflow())

    with mlflow_tracing.trace_model_call(name="embedding.batch", span_type="EMBEDDING", attributes={"model": "m"}):
        value = 41 + 1

    assert value == 42


def test_trace_model_call_marks_error_status(monkeypatch) -> None:
    span = _FakeSpan()
    fake_mlflow = _FakeMlflow(span)
    mlflow_tracing.configure_mlflow_tracing(enabled=True)
    monkeypatch.setattr(mlflow_tracing, "_load_mlflow", lambda: fake_mlflow)

    with (
        pytest.raises(ValueError, match="boom"),
        mlflow_tracing.trace_model_call(
            name="llm.chat.completions.batch",
            span_type="CHAT_MODEL",
            attributes={"model": "gpt-test"},
        ),
    ):
        raise ValueError("boom")

    assert span.attributes["status"] == "error"
    assert span.attributes["error_type"] == "ValueError"
