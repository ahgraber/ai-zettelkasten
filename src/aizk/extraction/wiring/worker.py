"""Run the extraction worker with an eagerly constructed NER extractor.

Missing NER dependencies or local model weights fail before the runner starts.
The worker also hosts admission when graph configuration enables it.
"""

from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING

from aizk.db.config import DatabaseConfig
from aizk.db.engine import get_engine
from aizk.extraction.core.extractors import EntityExtractor, Gliner2Extractor, SpacyExtractor
from aizk.extraction.handler import ExtractionStageHandler
from aizk.extraction.stage import extraction_adapter
from aizk.pipeline.admission import AdmissionConfig, AdmissionLoop
from aizk.pipeline.runner import DEFAULT_CANCEL_GRACE, StageRunner
from aizk.pipeline.stage import require_stale_threshold

if TYPE_CHECKING:
    from aizk.extraction.settings import ExtractionConfig

logger = logging.getLogger(__name__)


def build_extractor(config: "ExtractionConfig") -> EntityExtractor:
    """Build the injected NER extractor selected by ``config.extractor``.

    Raises:
        ImportError: If the selected extractor's pinned dependency or model
            artifact is unavailable (see :class:`~aizk.extraction.core.extractors.SpacyExtractor`
            / :class:`~aizk.extraction.core.extractors.Gliner2Extractor`).
    """
    if config.extractor == "spacy":
        return SpacyExtractor()
    return Gliner2Extractor()


def run_extraction_worker(
    extraction_config: "ExtractionConfig",
    admission_config: "AdmissionConfig | None" = None,
) -> int:
    """Run the extraction stage until shutdown.

    The function builds the configured handler and extractor, then returns the
    shared runner's exit code. Admission stops before the worker returns.

    Raises:
        StartupValidationError: If the stale threshold does not exceed the
            timeout plus the drain timeout and the cancellation allowance.
    """
    require_stale_threshold(
        stale_after=datetime.timedelta(minutes=extraction_config.worker_stale_minutes),
        timeout=datetime.timedelta(seconds=extraction_config.worker_timeout_seconds),
        drain_timeout=extraction_config.worker_drain_timeout_seconds,
        cancel_grace=DEFAULT_CANCEL_GRACE,
    )
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
    adapter = extraction_adapter(admission_config)
    admission = AdmissionLoop(engine, adapter, interval_seconds=admission_config.admission_interval_seconds)
    with admission:
        return runner.run()
