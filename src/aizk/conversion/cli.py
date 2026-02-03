"""CLI entrypoint for conversion service operations."""

from __future__ import annotations

import argparse
import os
import sys

from setproctitle import setproctitle
import uvicorn

from aizk.conversion.api.main import create_app
from aizk.conversion.db import create_db_and_tables
from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.utilities.litestream import LitestreamManager


def _require_karakeep_env() -> None:
    required_keys = ("KARAKEEP_API_KEY", "KARAKEEP_BASE_URL")
    missing = [key for key in required_keys if not os.environ.get(key)]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")


def _cmd_db_init(_args: argparse.Namespace) -> int:
    """Initialize database tables."""
    setproctitle("docling-db-init")
    create_db_and_tables()
    return 0


def _cmd_serve(_args: argparse.Namespace) -> int:
    """Run the FastAPI server."""
    _require_karakeep_env()
    setproctitle("docling-api")
    config = ConversionConfig()
    LitestreamManager(config, role="api").start()
    uvicorn.run(
        create_app(),
        host=config.api_host,
        port=config.api_port,
        reload=config.api_reload,
    )
    return 0


def _cmd_worker(_args: argparse.Namespace) -> int:
    """Run the background worker."""
    _require_karakeep_env()
    setproctitle("docling-worker")
    config = ConversionConfig()
    LitestreamManager(config, role="worker").start()
    try:
        from aizk.conversion.workers.worker import run_worker
    except ImportError as exc:
        raise RuntimeError("Worker implementation is not available yet.") from exc
    run_worker()
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the conversion service CLI."""
    parser = argparse.ArgumentParser(prog="aizk-conversion")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("db-init").set_defaults(func=_cmd_db_init)
    subparsers.add_parser("serve").set_defaults(func=_cmd_serve)
    subparsers.add_parser("worker").set_defaults(func=_cmd_worker)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
