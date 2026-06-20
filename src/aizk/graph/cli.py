"""Command-line entrypoints for the contextualization (graph) stage.

Two commands, mirroring the conversion CLI's structure. ``main`` loads the
environment and configures structured logging once, before any command runs, so
every subcommand uses the identical logging procedure.

- ``worker`` drives the contextualization stage through the shared runner: set a
  descriptive process title, run migrations (the graph tables live in the
  conversion Alembic tree), then run the loop.
- ``serve`` runs the operator API (jobs monitor + content explorer) over uvicorn.

Neither command manages Litestream: the graph stage reuses the conversion
database, whose replication is owned by the conversion service (only the process
matching ``litestream_start_role`` replicates). Migrations are run by ``worker``
on startup or by ``aizk-conversion db-init``; ``serve`` does not migrate.

Registered as the ``aizk-graph`` console script; also runnable via
``python -m aizk.graph.cli``.
"""

from __future__ import annotations

import argparse
import logging

from setproctitle import setproctitle
import uvicorn

from aizk.conversion.migrations import run_migrations
from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.utilities.dotenv import load_process_dotenv_once
from aizk.conversion.utilities.logging import configure_logging
from aizk.conversion.utilities.startup import StartupValidationError
from aizk.graph.config import ContextualizationConfig
from aizk.graph.worker import run_graph_worker

logger = logging.getLogger(__name__)


def _cmd_serve(_args: argparse.Namespace) -> int:
    """Run the graph operator API (jobs monitor + content explorer) over uvicorn."""
    setproctitle("graph-operator-api")
    contextualization_config = ContextualizationConfig()
    # Factory import string: the app reads the shared config on its own listener,
    # distinct from the conversion API (hence a distinct default port).
    uvicorn.run(
        "aizk.graph.api.main:create_app",
        factory=True,
        host=contextualization_config.operator_api_host,
        port=contextualization_config.operator_api_port,
        reload=contextualization_config.operator_api_reload,
    )
    return 0


def _cmd_worker(_args: argparse.Namespace) -> int:
    """Run the contextualization worker until shutdown; return its exit code."""
    setproctitle("graph-contextualization-worker")
    conversion_config = ConversionConfig()
    contextualization_config = ContextualizationConfig()
    run_migrations()
    try:
        return run_graph_worker(conversion_config, contextualization_config)
    except StartupValidationError:
        logger.exception("startup validation failed", extra={"role": "graph-worker"})
        return 1


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch the contextualization CLI command."""
    load_process_dotenv_once()
    # One structured-logging setup for every subcommand, before any runs — the
    # same procedure the conversion CLI uses.
    configure_logging(ConversionConfig())
    parser = argparse.ArgumentParser(prog="aizk-graph")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("serve", help="Run the operator API (jobs monitor + content explorer).").set_defaults(
        func=_cmd_serve
    )
    subparsers.add_parser("worker", help="Run the contextualization worker loop.").set_defaults(func=_cmd_worker)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
