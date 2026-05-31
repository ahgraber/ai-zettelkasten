"""CLI entrypoint for conversion service operations."""

from __future__ import annotations

import argparse
import logging
import sys

from setproctitle import setproctitle
import uvicorn

from aizk.conversion.utilities.config import ConversionConfig, DoclingConverterConfig
from aizk.conversion.utilities.dotenv import load_process_dotenv_once
from aizk.conversion.utilities.litestream import LitestreamManager
from aizk.conversion.utilities.logging import configure_logging
from aizk.conversion.utilities.startup import StartupValidationError, log_feature_summary
from aizk.utilities.mlflow_tracing import configure_mlflow_tracing

logger = logging.getLogger(__name__)


def _cmd_db_init(_args: argparse.Namespace) -> int:
    """Initialize database tables via Alembic migrations."""
    setproctitle("docling-db-init")
    from aizk.conversion.migrations import run_migrations

    run_migrations()
    return 0


def _cmd_serve(_args: argparse.Namespace) -> int:
    """Run the FastAPI server."""
    setproctitle("docling-api")
    config = ConversionConfig()
    docling_cfg = DoclingConverterConfig()
    log_feature_summary(config, docling_cfg, "api")
    configure_mlflow_tracing(
        enabled=config.mlflow_tracing_enabled,
        tracking_uri=config.mlflow_tracking_uri,
        experiment_name=config.mlflow_experiment_name,
    )
    LitestreamManager(config, role="api").start()
    uvicorn.run(
        "aizk.conversion.api.main:app",
        host=config.api_host,
        port=config.api_port,
        reload=config.api_reload,
    )
    return 0


def _cmd_worker(_args: argparse.Namespace) -> int:
    """Run the background worker."""
    setproctitle("docling-worker")
    config = ConversionConfig()
    # Configure logging FIRST so every subsequent emission — including the
    # egress enforcement WARNING records that carry forensic ``extra`` keys
    # (url, host, ip, error_class, hop_index) — uses the structured
    # formatter. Without this the worker process emits via Python's
    # lastResort handler (stderr-only, default format), silently dropping
    # the audit trail the network-egress-policy design depends on.
    configure_logging(config)
    docling_cfg = DoclingConverterConfig()
    log_feature_summary(config, docling_cfg, "worker")
    configure_mlflow_tracing(
        enabled=config.mlflow_tracing_enabled,
        tracking_uri=config.mlflow_tracking_uri,
        experiment_name=config.mlflow_experiment_name,
    )
    LitestreamManager(config, role="worker").start()
    from aizk.conversion.migrations import run_migrations

    run_migrations()
    # The worker drives the conversion stage through the pipeline runner
    # (StageRunner + ConversionStageHandler). The runner's startup gate runs the
    # adapter-declared probes before claiming any work; a probe failure raises
    # StartupValidationError, which we map to a non-zero exit.
    from aizk.conversion.processing.worker import run_worker

    try:
        return run_worker(config)
    except StartupValidationError:
        logger.exception("startup validation failed", extra={"role": "worker"})
        return 1


def main(argv: list[str] | None = None) -> int:
    """Run the conversion service CLI."""
    load_process_dotenv_once()
    parser = argparse.ArgumentParser(prog="aizk-conversion")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("db-init").set_defaults(func=_cmd_db_init)
    subparsers.add_parser("serve").set_defaults(func=_cmd_serve)
    subparsers.add_parser("worker").set_defaults(func=_cmd_worker)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
