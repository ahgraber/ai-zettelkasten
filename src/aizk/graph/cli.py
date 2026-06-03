"""Command-line entrypoint for the contextualization (graph) worker.

Mirrors the conversion worker's startup sequence: set a descriptive process
title, load environment, configure structured logging, run migrations (the graph
tables live in the conversion Alembic tree), then drive the contextualization
stage through the shared runner. Registered as the ``aizk-graph`` console script;
also runnable via ``python -m aizk.graph.cli``.
"""

from __future__ import annotations

import argparse
import logging

from setproctitle import setproctitle

from aizk.conversion.migrations import run_migrations
from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.utilities.dotenv import load_process_dotenv_once
from aizk.conversion.utilities.logging import configure_logging
from aizk.conversion.utilities.startup import StartupValidationError
from aizk.graph.config import ContextualizationConfig
from aizk.graph.worker import run_graph_worker

logger = logging.getLogger(__name__)


def _cmd_worker(_args: argparse.Namespace) -> int:
    """Run the contextualization worker until shutdown; return its exit code."""
    setproctitle("graph-contextualization-worker")
    load_process_dotenv_once()
    conversion_config = ConversionConfig()
    contextualization_config = ContextualizationConfig()
    configure_logging(conversion_config)
    run_migrations()
    try:
        return run_graph_worker(conversion_config, contextualization_config)
    except StartupValidationError:
        logger.exception("startup validation failed", extra={"role": "graph-worker"})
        return 1


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch the contextualization CLI command."""
    parser = argparse.ArgumentParser(prog="aizk-graph")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("worker", help="Run the contextualization worker loop.").set_defaults(func=_cmd_worker)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
