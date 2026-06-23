"""Structured logging configuration for the conversion service.

The conversion code attaches forensic context to WARNING records via
``logger.warning(..., extra={...})`` — most notably at the egress enforcement
sites in :mod:`aizk.conversion.utilities.egress`,
:mod:`aizk.conversion.utilities.egress_fetch`,
:mod:`aizk.conversion.utilities.html_prefetch`, and
:mod:`aizk.conversion.utilities.paths`. Those ``extra`` keys (``url``, ``host``,
``ip``, ``error_class``, ``hop_index``, etc.) are the only operator-visible
record of which destination was rejected and why; the persisted
``error_message`` is intentionally sanitized to avoid echoing the rejected
destination back to API clients.

Both formatters in this module therefore pass through arbitrary ``extra``
keys, so no diagnostic field is silently dropped between emission and
operator-visible output.
"""

from __future__ import annotations

import json
import logging
from logging import LogRecord

from aizk.conversion.utilities.config import ConversionConfig

# Standard ``LogRecord`` attributes that should NEVER appear in formatter
# output — they are framework noise (``args``, ``msg``, ``pathname``, etc.) or
# metadata that the formatter renders explicitly (``message``, ``asctime``,
# ``levelname``, ``name``).  Computed once at import by snapshotting a stub
# record's ``__dict__``; keeps the set in sync with the stdlib across versions.
_RESERVED_RECORD_ATTRS: frozenset[str] = frozenset(
    LogRecord(name="", level=0, pathname="", lineno=0, msg="", args=(), exc_info=None).__dict__.keys()
) | {"message", "asctime"}

# Static context fields the structured logger always emits at top level.
_STATIC_CONTEXT_KEYS: tuple[str, ...] = ("source_id", "job_id", "karakeep_id", "status")


def _extract_extras(record: LogRecord, exclude: set[str]) -> dict[str, object]:
    """Return ``extra`` keys attached to ``record`` not already rendered elsewhere."""
    extras: dict[str, object] = {}
    for key, value in record.__dict__.items():
        if key in _RESERVED_RECORD_ATTRS or key in exclude:
            continue
        extras[key] = value
    return extras


class ContextFilter(logging.Filter):
    """Ensure structured context keys exist on log records."""

    def filter(self, record: LogRecord) -> bool:
        """Attach conversion context fields to the log record."""
        for key in _STATIC_CONTEXT_KEYS:
            if not hasattr(record, key):
                setattr(record, key, None)
        return True


class JsonFormatter(logging.Formatter):
    """Minimal JSON formatter that preserves arbitrary ``extra`` keys.

    Top-level fields:

    - ``timestamp``, ``level``, ``logger``, ``message`` — standard envelope.
    - ``source_id``, ``job_id``, ``karakeep_id``, ``status`` — static
      conversion-context keys (always present, possibly ``null``).
    - Any other ``extra`` key attached to the record (e.g. ``url``, ``host``,
      ``ip``, ``error_class``, ``hop_index``, ``configured_cap_bytes``,
      ``observed_size_bytes_bound``) — passed through verbatim.

    Non-JSON-serialisable values fall back to ``str()`` via ``json.dumps(..., default=str)``.
    """

    def format(self, record: LogRecord) -> str:
        """Format a record as a single-line JSON object."""
        payload: dict[str, object] = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in _STATIC_CONTEXT_KEYS:
            payload[key] = getattr(record, key, None)
        # Forensic extras: url, host, ip, error_class, hop_index, etc.
        # The H1 contract — "rejected destination SHALL be in WARNING logs" —
        # depends on this passthrough.
        payload.update(_extract_extras(record, exclude=set(payload.keys())))
        return json.dumps(payload, ensure_ascii=True, default=str)


class _TextFormatter(logging.Formatter):
    """Plain-text formatter that appends arbitrary ``extra`` keys.

    Renders the configured base format string, then appends a sorted
    ``key=value`` tail for any forensic extras the egress code attaches via
    ``logger.warning(..., extra={...})``.  Same passthrough contract as
    :class:`JsonFormatter` — H1 audit-log fields cannot be silently dropped.
    """

    _BASE_FORMAT = (
        "%(asctime)s %(levelname)s %(name)s %(message)s "
        "source_id=%(source_id)s job_id=%(job_id)s "
        "karakeep_id=%(karakeep_id)s status=%(status)s"
    )

    def __init__(self) -> None:
        """Initialize the plain-text formatter with the project's base format."""
        super().__init__(fmt=self._BASE_FORMAT)

    def format(self, record: LogRecord) -> str:
        """Render the base format, then append non-static ``extra`` keys."""
        rendered = super().format(record)
        # Exclude the static keys already rendered by the base format string.
        excluded = set(_STATIC_CONTEXT_KEYS)
        extras = _extract_extras(record, exclude=excluded)
        if extras:
            tail = " ".join(f"{key}={value!r}" for key, value in sorted(extras.items()))
            rendered = f"{rendered} {tail}"
        return rendered


class ContextLoggerAdapter(logging.LoggerAdapter):
    """Logger adapter that merges contextual fields."""

    def process(self, msg, kwargs):
        """Merge adapter context into log record metadata."""
        extra = kwargs.setdefault("extra", {})
        extra.update(self.extra)
        return msg, kwargs


def configure_logging(config: ConversionConfig) -> None:
    """Configure logging for the conversion service.

    Installs a single root-level :class:`logging.StreamHandler` with the
    :class:`ContextFilter` and the formatter chosen by
    ``config.log_format`` (``json`` → :class:`JsonFormatter`; anything else
    → :class:`_TextFormatter`). MUST be called once during process startup
    before any code that emits forensic ``extra`` fields runs — this includes
    the worker subprocess, where the egress enforcement sites live.
    """
    handler = logging.StreamHandler()
    handler.addFilter(ContextFilter())
    if config.log_format.lower() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(_TextFormatter())
    root = logging.getLogger()
    root.setLevel(config.log_level.upper())
    root.handlers = [handler]


def get_logger(name: str, **context) -> ContextLoggerAdapter:
    """Return a logger adapter with conversion context."""
    logger = logging.getLogger(name)
    return ContextLoggerAdapter(logger, context)
