"""Command-line entrypoints for the graph stage (contextualization + extraction).

Mirrors the conversion CLI's structure. ``main`` loads the environment and
configures structured logging once, before any command runs, so every subcommand
uses the identical logging procedure.

- ``worker`` drives the contextualization stage through the shared runner: set a
  descriptive process title, run migrations (graph tables live in the shared
  migration tree alongside every other stage's), then run the loop.
- ``extraction-worker`` drives the extraction stage through the same shared
  runner, as a separate process/subcommand: extraction and contextualization
  are independently claimable, scalable stages over their own work-unit
  tables, so they run as distinct worker processes rather than one process
  driving two runners.
- ``extract-dataset`` runs a **synchronous, foreground** extraction pass over an
  explicitly-selected target-source set (see
  :mod:`aizk.graph.dataset_extraction`) — distinct from ``extraction-worker``,
  which claims work-units through the shared runtime — and prints the
  resulting corpus mention dataset's cold-start statistics
  (:mod:`aizk.graph.dataset_stats`) as JSON on stdout. A corpus-scanning target
  selection (no ``--source-id``) requires ``--yes`` confirmation.
- ``backfill`` and ``extraction-backfill`` enqueue each stage's work-units over
  the corpus (see :mod:`aizk.graph.backfill`). They enqueue only — the units they
  create stay ``QUEUED`` until the matching worker claims them — and an implicit
  corpus-scan selection requires ``--yes`` confirmation.
- ``serve`` runs the operator API (jobs monitor + content explorer, for both
  stages) over uvicorn.
- ``fetch-gliner2-weights`` is the one-time setup step that pre-fetches the
  pinned GLiNER2 model weights into the local directory
  :class:`~aizk.graph.extraction.Gliner2Extractor` loads from; it never runs as
  part of a worker's startup, since a stage adapter must never reach the network
  for model weights.

No worker command manages Litestream: the graph stage reuses the conversion
database, whose replication is owned by the conversion service (only the
process matching ``litestream_start_role`` replicates). Migrations are run by
either worker (or ``extract-dataset``) on startup or by ``aizk-conversion
db-init``; ``serve`` does not migrate.

Registered as the ``aizk-graph`` console script; also runnable via
``python -m aizk.graph.cli``.
"""

from __future__ import annotations

import argparse
import logging
import sys
from uuid import UUID

from setproctitle import setproctitle
from sqlmodel import Session
import uvicorn

from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.utilities.dotenv import load_process_dotenv_once
from aizk.conversion.utilities.logging import configure_logging
from aizk.conversion.utilities.startup import StartupValidationError
from aizk.db.config import DatabaseConfig
from aizk.db.engine import get_engine
from aizk.db.migrations import run_migrations
from aizk.graph.backfill import BackfillResult, run_contextualization_backfill, run_extraction_backfill
from aizk.graph.capacity import StageAtCapacityError
from aizk.graph.config import AdmissionConfig, ContextualizationConfig, ExtractionConfig, NerConfig
from aizk.graph.dataset_extraction import run_dataset_extraction
from aizk.graph.dataset_stats import compute_dataset_statistics
from aizk.graph.extraction import GLINER2_REPO_ID
from aizk.graph.extraction_worker import build_extractor, run_extraction_worker
from aizk.graph.worker import run_graph_worker
from aizk.pipeline.invalidation import ReprocessingConfirmationError
from aizk.utilities.cli_gates import positive_int, refuse_unconfirmed

logger = logging.getLogger(__name__)


def _report_backfill(result: BackfillResult, *, stage: str, worker_command: str, dry_run: bool) -> None:
    """Print a backfill run's counts and name the worker that drains what it enqueued."""
    if dry_run:
        print(
            f"{stage} backfill (dry run): {result.targeted} targeted, "
            f"{result.enqueued} would be enqueued, {result.reused} already exist. "
            "No work-units were written."
        )
        return
    print(f"{stage} backfill: {result.targeted} targeted, {result.enqueued} enqueued, {result.reused} reused.")
    print(f"Enqueued work stays QUEUED until a worker drains it — start one with `{worker_command}`.")


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


def _cmd_fetch_gliner2_weights(_args: argparse.Namespace) -> int:
    """Pre-fetch the pinned GLiNER2 weights into the configured local model directory.

    Lazily imports ``huggingface_hub`` (it arrives with the ``ner`` dependency
    group) and downloads the exact pinned revision recorded in
    :class:`~aizk.graph.config.NerConfig`, so :class:`~aizk.graph.extraction.Gliner2Extractor`
    can load strictly from disk at runtime. A one-time setup step, not part of
    ``worker`` startup.
    """
    setproctitle("graph-fetch-gliner2-weights")
    ner_config = NerConfig()
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        logger.exception("huggingface_hub is required to fetch GLiNER2 weights; install the 'ner' dependency group")
        return 1
    snapshot_download(
        repo_id=GLINER2_REPO_ID, revision=ner_config.gliner2_revision, local_dir=ner_config.gliner2_model_dir
    )
    logger.info(
        "Fetched GLiNER2 weights",
        extra={
            "repo_id": GLINER2_REPO_ID,
            "revision": ner_config.gliner2_revision,
            "local_dir": ner_config.gliner2_model_dir,
        },
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


def _cmd_extraction_worker(_args: argparse.Namespace) -> int:
    """Run the extraction worker until shutdown; return its exit code.

    Gates on :class:`ImportError` rather than :class:`~aizk.conversion.utilities.startup.StartupValidationError`:
    the extraction stage's startup-time failure is its pinned extractor's
    dependency/model-artifact construction (see
    :func:`aizk.graph.extraction_worker.build_extractor`), which already
    raises :class:`ImportError` naming the fix, unlike contextualization's
    model-endpoint configuration gate.
    """
    setproctitle("graph-extraction-worker")
    extraction_config = ExtractionConfig()
    run_migrations()
    try:
        return run_extraction_worker(extraction_config)
    except ImportError:
        logger.exception("startup validation failed", extra={"role": "graph-extraction-worker"})
        return 1


def _cmd_backfill(args: argparse.Namespace) -> int:
    """Enqueue contextualization work-units over the corpus, or over named outputs.

    Migrates first (a backfill is often the first thing run against a fresh
    database), then delegates target selection and enqueue to
    :func:`~aizk.graph.backfill.run_contextualization_backfill`. Enqueue only: the
    units it creates stay ``QUEUED`` until ``aizk-graph worker`` claims them.

    A ``--output-id`` naming no conversion output is operator input, so it is
    reported as a usage error rather than raised as a traceback. A stage at its
    declared capacity is reported the same way: the backlog, not the command, is
    what has to change.
    """
    setproctitle("graph-backfill")
    run_migrations()
    engine = get_engine(DatabaseConfig().database_url)
    try:
        result = run_contextualization_backfill(
            engine,
            output_ids=args.output_id,
            limit=args.limit,
            confirmed=args.yes,
            dry_run=args.dry_run,
            queue_max_depth=AdmissionConfig().contextualization_queue_max_depth,
        )
    except ReprocessingConfirmationError:
        return refuse_unconfirmed("corpus-wide contextualization backfill", explicit_flag="--output-id")
    except (StageAtCapacityError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    _report_backfill(result, stage="Contextualization", worker_command="aizk-graph worker", dry_run=args.dry_run)
    return 0


def _cmd_extraction_backfill(args: argparse.Namespace) -> int:
    """Enqueue extraction work-units over the corpus, or over named sources.

    Migrates first, then delegates to
    :func:`~aizk.graph.backfill.run_extraction_backfill`. Enqueue only: the units
    it creates stay ``QUEUED`` until ``aizk-graph extraction-worker`` claims them.

    A stage at capacity is reported as a usage error rather than raised as a
    traceback: the backlog, not the command, is what has to change.
    """
    setproctitle("graph-extraction-backfill")
    run_migrations()
    engine = get_engine(DatabaseConfig().database_url)
    try:
        result = run_extraction_backfill(
            engine,
            source_ids=args.source_id,
            confirmed=args.yes,
            dry_run=args.dry_run,
            queue_max_depth=AdmissionConfig().extraction_queue_max_depth,
        )
    except ReprocessingConfirmationError:
        return refuse_unconfirmed("corpus-wide extraction backfill", explicit_flag="--source-id")
    except StageAtCapacityError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    _report_backfill(result, stage="Extraction", worker_command="aizk-graph extraction-worker", dry_run=args.dry_run)
    return 0


def _cmd_extract_dataset(args: argparse.Namespace) -> int:
    """Run a foreground extraction dataset pass, then print corpus cold-start statistics as JSON.

    Resolves the target-source set (explicit ``--source-id``, else a corpus
    scan optionally capped by ``--limit``; see
    :func:`aizk.graph.dataset_extraction.resolve_target_source_ids`), gates a
    corpus-scanning selection behind ``--yes`` confirmation, and runs
    :func:`~aizk.graph.extraction_run.extract_corpus` synchronously in this
    process — a foreground run, not a claim through the extraction worker's
    work-unit queue. Re-invoking over an unchanged corpus is a cheap no-op
    (each target source's extraction reuses its active run when its
    derivation key is unchanged). The printed statistics always describe the
    full corpus mention dataset — the union of every source's active
    extraction run — not only the sources this invocation extracted.
    """
    setproctitle("graph-extract-dataset")
    extraction_config = ExtractionConfig()
    run_migrations()
    try:
        extractor = build_extractor(extraction_config)
    except ImportError:
        logger.exception("startup validation failed", extra={"role": "graph-extract-dataset"})
        return 1

    engine = get_engine(DatabaseConfig().database_url)
    try:
        results = run_dataset_extraction(
            engine,
            source_ids=args.source_id,
            limit=args.limit,
            confirmed=args.yes,
            extractor=extractor,
            input_policy=extraction_config.input_policy,
        )
    except ReprocessingConfirmationError:
        return refuse_unconfirmed("corpus-scanning extraction dataset run", explicit_flag="--source-id")

    logger.info(
        "Extraction dataset run complete",
        extra={"source_count": len(results), "mention_count": sum(result.mention_count for result in results)},
    )
    with Session(engine) as session:
        stats = compute_dataset_statistics(session)
    print(stats.model_dump_json(indent=2))
    return 0


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
    subparsers.add_parser("extraction-worker", help="Run the extraction worker loop.").set_defaults(
        func=_cmd_extraction_worker
    )
    subparsers.add_parser(
        "fetch-gliner2-weights",
        help="Pre-fetch the pinned GLiNER2 model weights into the local model directory (one-time setup).",
    ).set_defaults(func=_cmd_fetch_gliner2_weights)
    backfill_parser = subparsers.add_parser(
        "backfill",
        help="Enqueue contextualization work-units for the corpus, or for named conversion outputs.",
    )
    backfill_parser.add_argument(
        "--output-id",
        action="append",
        type=int,
        default=None,
        metavar="OUTPUT_ID",
        help=(
            "Explicit target conversion output (repeatable). An explicit enumeration is deliberate "
            "operator intent, so it is never confirmation-gated; only an implicit corpus scan "
            "requires --yes."
        ),
    )
    backfill_parser.add_argument(
        "--limit",
        type=positive_int,
        default=None,
        metavar="N",
        help="Cap a corpus scan to its first N sources; ignored with --output-id.",
    )
    backfill_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve and report the target set without persisting any work-unit.",
    )
    backfill_parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm an implicit (corpus-scan or --limit) target selection; not needed with --output-id.",
    )
    backfill_parser.set_defaults(func=_cmd_backfill)
    extraction_backfill_parser = subparsers.add_parser(
        "extraction-backfill",
        help="Enqueue extraction work-units for the corpus, or for named sources.",
    )
    extraction_backfill_parser.add_argument(
        "--source-id",
        action="append",
        type=UUID,
        default=None,
        metavar="SOURCE_ID",
        help=(
            "Explicit target source (repeatable). An explicit enumeration is deliberate operator "
            "intent, so it is never confirmation-gated; only an implicit corpus scan requires --yes."
        ),
    )
    extraction_backfill_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve and report the target set without persisting any work-unit.",
    )
    extraction_backfill_parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm an implicit (corpus-scan) target selection; not needed with --source-id.",
    )
    extraction_backfill_parser.set_defaults(func=_cmd_extraction_backfill)
    extract_dataset_parser = subparsers.add_parser(
        "extract-dataset",
        help=(
            "Run a foreground extraction pass over target sources and print the corpus mention "
            "dataset's cold-start statistics as JSON."
        ),
    )
    extract_dataset_parser.add_argument(
        "--source-id",
        action="append",
        default=None,
        metavar="SOURCE_ID",
        help=(
            "Explicit target source id (repeatable). An explicit enumeration is deliberate operator "
            "intent regardless of its length, so it is never confirmation-gated; only implicit "
            "selection (full corpus or --limit N) requires --yes."
        ),
    )
    extract_dataset_parser.add_argument(
        "--limit",
        type=positive_int,
        default=None,
        metavar="N",
        help="Cap a corpus-scan target selection to its first N sources; ignored with --source-id.",
    )
    extract_dataset_parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm an implicit (corpus-scan or --limit) target selection; not needed with --source-id.",
    )
    extract_dataset_parser.set_defaults(func=_cmd_extract_dataset)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
