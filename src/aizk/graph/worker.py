"""Run the contextualization worker with injected model and Markdown dependencies.

The worker requires a complete model endpoint and hosts the stage admission loop.
Admission remains disabled unless the graph configuration enables it.
"""

from __future__ import annotations

import contextlib
import datetime
import logging
from typing import TYPE_CHECKING

from aizk.ai.providers.llm import ModelConfig, build_llm_client
from aizk.db.config import DatabaseConfig
from aizk.db.engine import get_engine
from aizk.graph.admission import AdmissionLoop, admission_adapter_for
from aizk.graph.config import AdmissionConfig
from aizk.graph.events import CONTEXTUALIZATION_STAGE
from aizk.graph.handler import ContextualizationStageHandler
from aizk.graph.markdown_source import ConversionOutputFreshness, S3MarkdownSource
from aizk.pipeline.runner import DEFAULT_CANCEL_GRACE, StageRunner
from aizk.pipeline.stage import require_stale_threshold
from aizk.storage.s3 import S3Client
from aizk.utilities.errors import StartupValidationError

if TYPE_CHECKING:
    from aizk.conversion.utilities.config import ConversionConfig
    from aizk.graph.config import ContextualizationConfig

logger = logging.getLogger(__name__)


def run_graph_worker(
    conversion_config: "ConversionConfig",
    contextualization_config: "ContextualizationConfig",
    admission_config: "AdmissionConfig | None" = None,
) -> int:
    """Run the contextualization worker and its admission loop until shutdown.

    Raises:
        StartupValidationError: If the stage's model endpoint is incomplete, or
            its stale threshold does not exceed its timeout plus the drain
            timeout and the cancellation allowance.
    """
    if not contextualization_config.llm_enabled:
        raise StartupValidationError(
            "contextualization model endpoint is not configured; set "
            "AIZK_GRAPH__CONTEXTUALIZATION__LLM_BASE_URL, _LLM_API_KEY, and _LLM_MODEL"
        )
    require_stale_threshold(
        stale_after=datetime.timedelta(minutes=contextualization_config.worker_stale_minutes),
        timeout=datetime.timedelta(seconds=contextualization_config.worker_timeout_seconds),
        drain_timeout=contextualization_config.worker_drain_timeout_seconds,
        cancel_grace=DEFAULT_CANCEL_GRACE,
    )
    admission_config = admission_config if admission_config is not None else AdmissionConfig()
    engine = get_engine(DatabaseConfig().database_url)
    llm_client = build_llm_client(
        contextualization_config.llm_base_url,
        contextualization_config.llm_api_key,
        contextualization_config.llm_model,
    )
    markdown_source = S3MarkdownSource(engine, S3Client(conversion_config))
    model_config = ModelConfig.from_contextualization_config(contextualization_config)

    handler = ContextualizationStageHandler(
        engine,
        llm_client,
        markdown_source,
        ConversionOutputFreshness(),
        model_config=model_config,
        timeout_seconds=contextualization_config.worker_timeout_seconds,
        concurrency=contextualization_config.worker_concurrency,
        stale_after_minutes=contextualization_config.worker_stale_minutes,
        retry_base_delay_seconds=contextualization_config.retry_base_delay_seconds,
        retry_max_attempts=contextualization_config.retry_max_attempts,
    )
    runner = StageRunner(
        handler,
        engine,
        drain_timeout=contextualization_config.worker_drain_timeout_seconds,
        poll_interval=contextualization_config.worker_poll_interval_seconds,
        stale_recovery_interval=contextualization_config.worker_stale_recovery_interval_seconds,
    )
    logger.info(
        "Starting contextualization worker (model=%s concurrency=%d)",
        contextualization_config.llm_model,
        contextualization_config.worker_concurrency,
    )
    adapter = admission_adapter_for(CONTEXTUALIZATION_STAGE, admission_config)
    admission = (
        AdmissionLoop(engine, adapter, interval_seconds=admission_config.admission_interval_seconds)
        if adapter is not None
        else contextlib.nullcontext()
    )
    with admission:
        return runner.run()
