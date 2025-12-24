"""Structured logging configuration for the conversion service."""

from __future__ import annotations

import json
import logging
from logging import LogRecord

from aizk.conversion.utilities.config import ConversionConfig


class ContextFilter(logging.Filter):
    """Ensure structured context keys exist on log records."""

    def filter(self, record: LogRecord) -> bool:
        """Attach conversion context fields to the log record."""
        for key in ("aizk_uuid", "job_id", "karakeep_id", "status"):
            if not hasattr(record, key):
                setattr(record, key, None)
        return True


class JsonFormatter(logging.Formatter):
    """Minimal JSON formatter with context fields."""

    def format(self, record: LogRecord) -> str:
        """Format log records into a JSON payload."""
        payload = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "aizk_uuid": getattr(record, "aizk_uuid", None),
            "job_id": getattr(record, "job_id", None),
            "karakeep_id": getattr(record, "karakeep_id", None),
            "status": getattr(record, "status", None),
        }
        return json.dumps(payload, ensure_ascii=True)


class ContextLoggerAdapter(logging.LoggerAdapter):
    """Logger adapter that merges contextual fields."""

    def process(self, msg, kwargs):
        """Merge adapter context into log record metadata."""
        extra = kwargs.setdefault("extra", {})
        extra.update(self.extra)
        return msg, kwargs


def configure_logging(config: ConversionConfig) -> None:
    """Configure logging for the conversion service."""
    handler = logging.StreamHandler()
    handler.addFilter(ContextFilter())
    if config.log_format.lower() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        fmt = (
            "%(asctime)s %(levelname)s %(name)s %(message)s "
            "aizk_uuid=%(aizk_uuid)s job_id=%(job_id)s "
            "karakeep_id=%(karakeep_id)s status=%(status)s"
        )
        handler.setFormatter(logging.Formatter(fmt))
    root = logging.getLogger()
    root.setLevel(config.log_level.upper())
    root.handlers = [handler]


def get_logger(name: str, **context) -> ContextLoggerAdapter:
    """Return a logger adapter with conversion context."""
    logger = logging.getLogger(name)
    return ContextLoggerAdapter(logger, context)
