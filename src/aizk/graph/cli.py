"""Command-line entrypoints for the graph stage (contextualization + extraction).

Five commands, mirroring the conversion CLI's structure. ``main`` loads the
environment and configures structured logging once, before any command runs, so
every subcommand uses the identical logging procedure.

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
from aizk.graph.config import ContextualizationConfig, ExtractionConfig, NerConfig
from aizk.graph.dataset_extraction import run_dataset_extraction
from aizk.graph.dataset_stats import compute_dataset_statistics
from aizk.graph.extraction import GLINER2_REPO_ID
from aizk.graph.extraction_worker import build_extractor, run_extraction_worker
from aizk.graph.worker import run_graph_worker
from aizk.pipeline.invalidation import ReprocessingConfirmationError

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
        logger.exception("extraction dataset run refused", extra={"role": "graph-extract-dataset"})
        return 1

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
        type=int,
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
