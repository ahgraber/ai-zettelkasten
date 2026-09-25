"""CLI entrypoint for conversion service operations."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import httpx2
import uvicorn

from aizk.conversion.backfill import (
    ConversionApiUnreachableError,
    ConversionBackfillResult,
    resolve_conversion_api_base_url,
    run_conversion_backfill,
)
from aizk.conversion.settings import ConversionConfig, DoclingConverterConfig, KarakeepFetcherConfig
from aizk.conversion.wiring.probes import log_feature_summary
from aizk.db.backends.sqlite import LitestreamManager, SqliteDurabilityConfig
from aizk.service.bootstrap import set_process_title
from aizk.service.cli import positive_int, prepare_process, refuse_unconfirmed
from aizk.service.settings import ServiceSettings
from aizk.storage.settings import ObjectStorageSettings
from aizk.utilities.errors import StartupValidationError
from karakeep_client.karakeep import KarakeepClient

logger = logging.getLogger(__name__)

_BACKFILL_TIMEOUT_SECONDS = 30.0


def _cmd_db_init(_args: argparse.Namespace) -> int:
    """Initialize database tables via Alembic migrations."""
    set_process_title("docling-db-init")
    # Imported here: tests patch aizk.db.migrations.run_migrations at its definition.
    from aizk.db.migrations import run_migrations

    run_migrations()
    return 0


def _cmd_serve(_args: argparse.Namespace) -> int:
    """Run the FastAPI server."""
    set_process_title("docling-api")
    docling_cfg = DoclingConverterConfig()
    settings = ServiceSettings()
    log_feature_summary(settings, docling_cfg, "api")
    LitestreamManager(
        SqliteDurabilityConfig(),
        role="api",
        object_storage=ObjectStorageSettings(),
    ).start()
    uvicorn.run(
        "aizk.conversion.api.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.api_reload,
    )
    return 0


def _cmd_worker(_args: argparse.Namespace) -> int:
    """Run the background worker."""
    set_process_title("docling-worker")
    config = ConversionConfig()
    docling_cfg = DoclingConverterConfig()
    object_storage = ObjectStorageSettings()
    log_feature_summary(ServiceSettings(), docling_cfg, "worker")
    LitestreamManager(
        SqliteDurabilityConfig(),
        role="worker",
        object_storage=object_storage,
    ).start()
    # Imported here: tests patch aizk.db.migrations.run_migrations at its definition.
    from aizk.db.migrations import run_migrations

    run_migrations()
    # The worker drives the conversion stage through the pipeline runner
    # (StageRunner + ConversionStageHandler). The runner's startup gate runs the
    # adapter-declared probes before claiming any work; a probe failure raises
    # StartupValidationError, which we map to a non-zero exit.
    # Imported here: keeps docling and torch off the CLI's import path.
    from aizk.conversion.processing.worker import run_worker

    try:
        return run_worker(config, object_storage=object_storage)
    except StartupValidationError:
        logger.exception("startup validation failed", extra={"role": "worker"})
        return 1


async def _run_backfill(args: argparse.Namespace) -> ConversionBackfillResult:
    """Build the API and KaraKeep clients for one backfill run and drive it."""
    karakeep_config = KarakeepFetcherConfig()
    karakeep_client = KarakeepClient(api_key=karakeep_config.api_key, base_url=karakeep_config.base_url)
    base_url = resolve_conversion_api_base_url(ServiceSettings())
    async with httpx2.AsyncClient(base_url=base_url, timeout=_BACKFILL_TIMEOUT_SECONDS) as http_client:
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
    set_process_title("docling-backfill")
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
    prepare_process()
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
