"""CLI entrypoint for conversion service operations."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import httpx
from setproctitle import setproctitle
import uvicorn

from aizk.conversion.backfill import (
    ConversionApiUnreachableError,
    ConversionBackfillResult,
    resolve_conversion_api_base_url,
    run_conversion_backfill,
)
from aizk.conversion.utilities.config import ConversionConfig, DoclingConverterConfig, KarakeepFetcherConfig
from aizk.conversion.utilities.dotenv import load_process_dotenv_once
from aizk.conversion.utilities.logging import configure_logging
from aizk.conversion.utilities.startup import StartupValidationError, log_feature_summary
from aizk.db.backends.sqlite import LitestreamManager, SqliteDurabilityConfig
from aizk.utilities.cli_gates import positive_int, refuse_unconfirmed
from aizk.utilities.mlflow_tracing import configure_mlflow_tracing
from karakeep_client.karakeep import KarakeepClient

logger = logging.getLogger(__name__)

_BACKFILL_TIMEOUT_SECONDS = 30.0


def _cmd_db_init(_args: argparse.Namespace) -> int:
    """Initialize database tables via Alembic migrations."""
    setproctitle("docling-db-init")
    from aizk.db.migrations import run_migrations

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
    LitestreamManager(SqliteDurabilityConfig(), role="api").start()
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
    docling_cfg = DoclingConverterConfig()
    log_feature_summary(config, docling_cfg, "worker")
    configure_mlflow_tracing(
        enabled=config.mlflow_tracing_enabled,
        tracking_uri=config.mlflow_tracking_uri,
        experiment_name=config.mlflow_experiment_name,
    )
    LitestreamManager(SqliteDurabilityConfig(), role="worker").start()
    from aizk.db.migrations import run_migrations

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


async def _run_backfill(args: argparse.Namespace) -> ConversionBackfillResult:
    """Build the API and KaraKeep clients for one backfill run and drive it."""
    config = ConversionConfig()
    karakeep_config = KarakeepFetcherConfig()
    karakeep_client = KarakeepClient(api_key=karakeep_config.api_key, base_url=karakeep_config.base_url)
    base_url = resolve_conversion_api_base_url(config)
    async with httpx.AsyncClient(base_url=base_url, timeout=_BACKFILL_TIMEOUT_SECONDS) as http_client:
        return await run_conversion_backfill(
            http_client=http_client,
            karakeep_client=karakeep_client,
            page_size=args.page_size,
            limit=args.limit,
            dry_run=args.dry_run,
        )


def _cmd_backfill(args: argparse.Namespace) -> int:
    """Submit every KaraKeep bookmark to the conversion API.

    Submission goes through the API rather than the database so the endpoint's
    source materialization, idempotency key, and queue admission all apply. A
    run that could not submit some bookmarks exits non-zero, so a scripted
    backfill does not read as clean when part of the corpus was dropped.

    A sweep is corpus-wide reprocessing and is confirmation-gated. The API's
    idempotency key folds in the converter's configuration snapshot, so a
    converter version or config change gives every bookmark a fresh key and the
    same sweep re-converts the entire corpus. A dry run submits nothing and so
    needs no confirmation.
    """
    setproctitle("docling-backfill")
    if not args.yes and not args.dry_run:
        return refuse_unconfirmed("corpus-wide conversion backfill", explicit_flag=None)
    try:
        result = asyncio.run(_run_backfill(args))
    except (ConversionApiUnreachableError, ValueError) as exc:
        # ValueError covers an unconfigured KaraKeep client: a configuration
        # error the operator fixes, not a defect worth a traceback.
        print(str(exc), file=sys.stderr)
        return 1

    if args.dry_run:
        print(f"Conversion backfill (dry run): {result.submitted} bookmarks would be submitted.")
        return 0
    print(
        f"Conversion backfill: {result.submitted} submitted, {result.existing} already queued, {result.failed} failed."
    )
    print("Submitted work stays QUEUED until a worker drains it — start one with `aizk-conversion worker`.")
    return 1 if result.failed else 0


def main(argv: list[str] | None = None) -> int:
    """Run the conversion service CLI."""
    load_process_dotenv_once()
    # Configure logging once, before any command runs, so every subsequent
    # emission — including the egress-enforcement WARNING records that carry
    # forensic ``extra`` keys (url, host, ip, error_class, hop_index) — uses the
    # structured formatter. Without this a command would emit via Python's
    # lastResort handler (stderr-only, default format), silently dropping the
    # audit trail the network-egress-policy design depends on.
    configure_logging(ConversionConfig())
    parser = argparse.ArgumentParser(prog="aizk-conversion")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("db-init").set_defaults(func=_cmd_db_init)
    subparsers.add_parser("serve").set_defaults(func=_cmd_serve)
    subparsers.add_parser("worker").set_defaults(func=_cmd_worker)

    backfill_parser = subparsers.add_parser(
        "backfill",
        help="Submit every KaraKeep bookmark to the conversion API (requires a running `serve`).",
    )
    backfill_parser.add_argument(
        "--limit",
        type=positive_int,
        default=None,
        metavar="N",
        help="Stop after submitting N bookmarks.",
    )
    backfill_parser.add_argument(
        "--page-size",
        type=positive_int,
        default=100,
        metavar="N",
        help="KaraKeep page size (its maximum is 100).",
    )
    backfill_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Page KaraKeep and report the count without submitting anything.",
    )
    backfill_parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm the corpus-wide sweep; required for any run that submits.",
    )
    backfill_parser.set_defaults(func=_cmd_backfill)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
