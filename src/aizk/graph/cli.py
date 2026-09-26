"""Provide the ``aizk-graph`` commands for workers, serving, backfills, and dataset extraction.

Workers run the graph stages, backfills enqueue work, ``serve`` starts the
operator API, and ``extract-dataset`` runs a foreground extraction pass. Corpus
scans require explicit confirmation. Commands set descriptive process titles at
their process boundaries.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import sys
from uuid import UUID

from sqlmodel import Session
import uvicorn

from aizk.contextualization.backfill import run_contextualization_backfill
from aizk.contextualization.settings import ContextualizationConfig
from aizk.contextualization.wiring.worker import run_graph_worker
from aizk.db.config import DatabaseConfig
from aizk.db.engine import get_engine
from aizk.db.migrations import run_migrations
from aizk.extraction.backfill import run_extraction_backfill
from aizk.extraction.core.dataset_stats import compute_dataset_statistics
from aizk.extraction.core.extractors import GLINER2_REPO_ID
from aizk.extraction.processing.corpus import run_dataset_extraction
from aizk.extraction.settings import ExtractionConfig, NerConfig
from aizk.extraction.wiring.worker import build_extractor, run_extraction_worker
from aizk.graph.backfill import BackfillResult
from aizk.pipeline.admission import AdmissionConfig
from aizk.pipeline.capacity import StageAtCapacityError
from aizk.pipeline.invalidation import ReprocessingConfirmationError
from aizk.service.bootstrap import set_process_title
from aizk.service.cli import positive_int, prepare_process, refuse_unconfirmed
from aizk.service.settings import ServiceSettings
from aizk.storage.settings import ObjectStorageSettings
from aizk.utilities.errors import StartupValidationError

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
    set_process_title("graph-operator-api")
    operator_api = ServiceSettings().operator_api
    # Factory import string: the app reads the shared config on its own listener,
    # distinct from the conversion API (hence a distinct default port).
    uvicorn.run(
        "aizk.console.app:create_app",
        factory=True,
        host=operator_api.host,
        port=operator_api.port,
        reload=operator_api.reload,
    )
    return 0


def _cmd_fetch_gliner2_weights(_args: argparse.Namespace) -> int:
    """Download the pinned GLiNER2 weights into the configured local directory.

    The command downloads the revision in :class:`~aizk.extraction.settings.NerConfig`;
    workers load the weights from disk.
    """
    set_process_title("graph-fetch-gliner2-weights")
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
    set_process_title("graph-contextualization-worker")
    object_storage = ObjectStorageSettings()
    contextualization_config = ContextualizationConfig()
    run_migrations()
    try:
        return run_graph_worker(object_storage, contextualization_config)
    except StartupValidationError:
        logger.exception("startup validation failed", extra={"role": "graph-worker"})
        return 1


def _cmd_extraction_worker(_args: argparse.Namespace) -> int:
    """Run the extraction worker until shutdown and return its exit code.

    Return 1 when extractor setup raises ``ImportError``.
    """
    set_process_title("graph-extraction-worker")
    extraction_config = ExtractionConfig()
    run_migrations()
    try:
        return run_extraction_worker(extraction_config)
    except ImportError:
        logger.exception("startup validation failed", extra={"role": "graph-extraction-worker"})
        return 1


def _cmd_backfill(args: argparse.Namespace) -> int:
    """Enqueue contextualization work-units for named outputs or a corpus scan.

    The command runs migrations first and leaves created units ``QUEUED``. A
    corpus scan requires ``--yes``; invalid output ids and capacity refusals exit
    with a usage error.
    """
    set_process_title("graph-backfill")
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
            principal=getpass.getuser(),
        )
    except ReprocessingConfirmationError:
        return refuse_unconfirmed("corpus-wide contextualization backfill", explicit_flag="--output-id")
    except (StageAtCapacityError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    _report_backfill(result, stage="Contextualization", worker_command="aizk-graph worker", dry_run=args.dry_run)
    return 0


def _cmd_extraction_backfill(args: argparse.Namespace) -> int:
    """Enqueue extraction work-units for named sources or a corpus scan.

    The command runs migrations first and leaves created units ``QUEUED``. A
    corpus scan requires ``--yes``; capacity refusals exit with a usage error.
    """
    set_process_title("graph-extraction-backfill")
    run_migrations()
    engine = get_engine(DatabaseConfig().database_url)
    try:
        result = run_extraction_backfill(
            engine,
            source_ids=args.source_id,
            confirmed=args.yes,
            dry_run=args.dry_run,
            queue_max_depth=AdmissionConfig().extraction_queue_max_depth,
            principal=getpass.getuser(),
        )
    except ReprocessingConfirmationError:
        return refuse_unconfirmed("corpus-wide extraction backfill", explicit_flag="--source-id")
    except StageAtCapacityError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    _report_backfill(result, stage="Extraction", worker_command="aizk-graph extraction-worker", dry_run=args.dry_run)
    return 0


def _cmd_extract_dataset(args: argparse.Namespace) -> int:
    """Run a foreground extraction dataset pass and print corpus statistics as JSON.

    Explicit source selections run without confirmation. Corpus scans require
    ``--yes`` and may use ``--limit``. Statistics cover all active extraction runs,
    not only sources selected for this invocation.
    """
    set_process_title("graph-extract-dataset")
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


def _add_backfill_arguments(parser: argparse.ArgumentParser, *, yes_help: str) -> None:
    """Add the ``--dry-run`` and ``--yes`` options both backfill commands take."""
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve and report the target set without persisting any work-unit.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help=yes_help,
    )


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch the contextualization CLI command."""
    prepare_process()
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
    _add_backfill_arguments(
        backfill_parser,
        yes_help="Confirm an implicit (corpus-scan or --limit) target selection; not needed with --output-id.",
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
    _add_backfill_arguments(
        extraction_backfill_parser,
        yes_help="Confirm an implicit (corpus-scan) target selection; not needed with --source-id.",
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
