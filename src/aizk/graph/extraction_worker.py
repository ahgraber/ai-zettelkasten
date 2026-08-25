"""Composition root for the extraction worker.

Assembles the injected NER extractor (spaCy or GLiNER2, selected by
:class:`~aizk.graph.config.ExtractionConfig`), wires it into the
:class:`~aizk.graph.extraction_handler.ExtractionStageHandler`, and drives it
through the shared :class:`~aizk.pipeline.runner.StageRunner`. Resolves the
engine from the shared database foundation the graph stage already reuses
from conversion (the extraction stage's tables live in that same database).

The pinned extractor is constructed eagerly, before the runner starts:
:class:`~aizk.graph.extraction.SpacyExtractor` and
:class:`~aizk.graph.extraction.Gliner2Extractor` both raise
:class:`ImportError` naming the fix (the ``ner`` dependency group, or the
one-time ``aizk-graph fetch-gliner2-weights`` pre-fetch) when their pinned
dependency or model artifact is unavailable, so the worker refuses to start
rather than claiming work it cannot complete.

The process also hosts the stage's admission loop
(:class:`~aizk.graph.admission.AdmissionLoop`), so admission needs no process of
its own. It admits nothing unless automatic admission is switched on for the stage.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aizk.db.config import DatabaseConfig
from aizk.db.engine import get_engine
from aizk.graph.admission import AdmissionLoop, extraction_adapter
from aizk.graph.config import AdmissionConfig
from aizk.graph.extraction import EntityExtractor, Gliner2Extractor, SpacyExtractor
from aizk.graph.extraction_handler import ExtractionStageHandler
from aizk.pipeline.runner import StageRunner

if TYPE_CHECKING:
    from aizk.graph.config import ExtractionConfig

logger = logging.getLogger(__name__)


def build_extractor(config: "ExtractionConfig") -> EntityExtractor:
    """Build the injected NER extractor selected by ``config.extractor``.

    Raises:
        ImportError: If the selected extractor's pinned dependency or model
            artifact is unavailable (see :class:`~aizk.graph.extraction.SpacyExtractor`
            / :class:`~aizk.graph.extraction.Gliner2Extractor`).
    """
    if config.extractor == "spacy":
        return SpacyExtractor()
    return Gliner2Extractor()


def run_extraction_worker(
    extraction_config: "ExtractionConfig",
    admission_config: "AdmissionConfig | None" = None,
) -> int:
    """Build the extraction stage handler and drive it via the shared runner.

    Reuses the conversion/graph database engine; constructs the configured
    extractor; runs the supervised claim/execute/finalize loop until shutdown.
    Returns the runner's exit code.

    The stage's admission loop runs alongside, stopped and joined before the worker
    returns.
    """
    admission_config = admission_config if admission_config is not None else AdmissionConfig()
    engine = get_engine(DatabaseConfig().database_url)
    extractor = build_extractor(extraction_config)

    handler = ExtractionStageHandler(
        engine,
        extractor,
        input_policy=extraction_config.input_policy,
        timeout_seconds=extraction_config.worker_timeout_seconds,
        concurrency=extraction_config.worker_concurrency,
        stale_after_minutes=extraction_config.worker_stale_minutes,
        retry_base_delay_seconds=extraction_config.retry_base_delay_seconds,
        retry_max_attempts=extraction_config.retry_max_attempts,
    )
    runner = StageRunner(
        handler,
        engine,
        drain_timeout=extraction_config.worker_drain_timeout_seconds,
        poll_interval=extraction_config.worker_poll_interval_seconds,
        stale_recovery_interval=extraction_config.worker_stale_recovery_interval_seconds,
    )
    logger.info(
        "Starting extraction worker (extractor=%s input_policy=%s concurrency=%d)",
        extraction_config.extractor,
        extraction_config.input_policy,
        extraction_config.worker_concurrency,
    )
    admission = AdmissionLoop(
        engine,
        extraction_adapter(admission_config),
        interval_seconds=admission_config.admission_interval_seconds,
    )
    with admission:
        return runner.run()
