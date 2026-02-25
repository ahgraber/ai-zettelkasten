"""Best-effort MLflow tracing helpers for upstream model calls."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import ExitStack, contextmanager
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

_SENSITIVE_KEY_FRAGMENTS = (
    "api_key",
    "authorization",
    "secret",
    "token",
    "password",
    "credential",
)
_RAW_PAYLOAD_KEYS = ("prompt", "input", "text", "content", "messages", "body")

_tracing_enabled = False
_tracking_uri = ""
_experiment_name = ""
_runtime_configured = False
_mlflow_import_failed = False


def configure_mlflow_tracing(*, enabled: bool, tracking_uri: str = "", experiment_name: str = "") -> None:
    """Configure process-local MLflow tracing settings."""
    global _tracing_enabled, _tracking_uri, _experiment_name, _runtime_configured

    _tracing_enabled = enabled
    _tracking_uri = tracking_uri
    _experiment_name = experiment_name
    _runtime_configured = False


def _is_enabled() -> bool:
    """Return whether tracing is enabled via configured state or environment."""
    if _tracing_enabled:
        return True
    raw = os.getenv("MLFLOW_TRACING_ENABLED", "")
    return raw.lower() in {"1", "true", "yes", "on"}


def _env_or_configured(name: str, configured_value: str) -> str:
    """Return configured value or environment fallback."""
    if configured_value:
        return configured_value
    return os.getenv(name, "")


def sanitize_trace_attributes(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a sanitized copy of trace attributes."""
    if not attributes:
        return {}

    sanitized: dict[str, Any] = {}
    for key, value in attributes.items():
        normalized_key = key.lower()
        if any(fragment in normalized_key for fragment in _SENSITIVE_KEY_FRAGMENTS):
            continue
        if normalized_key in _RAW_PAYLOAD_KEYS:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            sanitized[key] = value
        else:
            sanitized[key] = str(value)
    return sanitized


def _load_mlflow() -> Any | None:
    """Load the MLflow module if available."""
    global _mlflow_import_failed

    if _mlflow_import_failed:
        return None
    try:
        import mlflow
    except ImportError:
        _mlflow_import_failed = True
        logger.warning("MLflow tracing disabled because mlflow is not installed")
        return None
    return mlflow


def _ensure_runtime_configured(mlflow_module: Any) -> None:
    """Apply runtime MLflow settings once per process."""
    global _runtime_configured

    if _runtime_configured:
        return

    tracking_uri = _env_or_configured("MLFLOW_TRACKING_URI", _tracking_uri)
    experiment_name = _env_or_configured("MLFLOW_EXPERIMENT_NAME", _experiment_name)

    if tracking_uri:
        mlflow_module.set_tracking_uri(tracking_uri)
    if experiment_name:
        mlflow_module.set_experiment(experiment_name)
    _runtime_configured = True


def _safe_set_attributes(span: Any | None, attributes: Mapping[str, Any]) -> None:
    """Best-effort attribute update on a span."""
    if span is None:
        return
    try:
        span.set_attributes(dict(attributes))
    except Exception:
        logger.debug("Failed setting MLflow span attributes", exc_info=True)


@contextmanager
def trace_model_call(*, name: str, span_type: str, attributes: Mapping[str, Any] | None = None):
    """Create a best-effort model-call span that never changes business behavior."""
    started = time.perf_counter()
    span = None
    error: Exception | None = None

    if not _is_enabled():
        yield None
        return

    mlflow_module = _load_mlflow()
    if mlflow_module is None:
        yield None
        return

    sanitized_attrs = sanitize_trace_attributes(attributes)

    with ExitStack() as stack:
        try:
            _ensure_runtime_configured(mlflow_module)
            span = stack.enter_context(
                mlflow_module.start_span(
                    name=name,
                    span_type=span_type,
                    attributes=sanitized_attrs,
                )
            )
        except Exception as exc:
            logger.warning("MLflow tracing span creation failed for %s: %s", name, exc)
            span = None

        try:
            yield span
        except Exception as exc:
            error = exc
            _safe_set_attributes(
                span,
                {
                    "status": "error",
                    "error_type": type(exc).__name__,
                },
            )
            raise
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 3)
            _safe_set_attributes(
                span,
                {
                    "status": "error" if error else "ok",
                    "latency_ms": duration_ms,
                },
            )
