"""Composition root for the contextualization worker.

Assembles the injected dependencies — the ``pydantic-ai`` model client and the
S3-backed Markdown source — wires them into the
:class:`~aizk.graph.handler.ContextualizationStageHandler`, and drives it through
the shared :class:`~aizk.pipeline.runner.StageRunner`. Resolves the engine from the
shared database foundation and reuses the conversion S3 client (the graph tables
share the database and the Markdown it reads is a conversion output).

The model endpoint is required: :func:`build_llm_client` raises
:class:`~aizk.conversion.utilities.startup.StartupValidationError` when the
OpenAI-compatible triple is not fully configured, so the worker refuses to start
rather than claiming work it cannot complete.

The process also hosts the stage's admission loop
(:class:`~aizk.graph.admission.AdmissionLoop`), which creates the work-units the
stage's upstream state says should exist. It runs alongside the claim/execute
loop rather than in a process of its own, and admits nothing unless automatic
admission is switched on for the stage.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aizk.conversion.storage.s3_client import S3Client
from aizk.conversion.utilities.startup import StartupValidationError
from aizk.db.config import DatabaseConfig
from aizk.db.engine import get_engine
from aizk.graph.admission import AdmissionLoop, contextualization_adapter
from aizk.graph.config import AdmissionConfig
from aizk.graph.handler import ContextualizationStageHandler
from aizk.graph.llm import PydanticAILLMClient
from aizk.graph.markdown_source import ConversionOutputFreshness, S3MarkdownSource
from aizk.pipeline.runner import StageRunner

if TYPE_CHECKING:
    from aizk.conversion.utilities.config import ConversionConfig
    from aizk.graph.config import ContextualizationConfig

logger = logging.getLogger(__name__)


def build_llm_client(config: "ContextualizationConfig") -> PydanticAILLMClient:
    """Build the injected model client from the OpenAI-compatible endpoint triple.

    Raises:
        StartupValidationError: If the base-url / api-key / model triple is not
            fully configured (contextualization cannot run without a model).
    """
    if not config.llm_enabled:
        raise StartupValidationError(
            "contextualization model endpoint is not configured; set "
            "AIZK_GRAPH__CONTEXTUALIZATION__LLM_BASE_URL, _LLM_API_KEY, and _LLM_MODEL"
        )
    from pydantic_ai import Agent
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    model = OpenAIChatModel(
        config.llm_model,
        provider=OpenAIProvider(base_url=config.llm_base_url, api_key=config.llm_api_key),
    )
    return PydanticAILLMClient(Agent(model))


def run_graph_worker(
    conversion_config: "ConversionConfig",
    contextualization_config: "ContextualizationConfig",
    admission_config: "AdmissionConfig | None" = None,
) -> int:
    """Build the contextualization stage handler and drive it via the shared runner.

    Reuses the conversion database engine and S3 client; constructs the model
    client and Markdown source; runs the supervised claim/execute/finalize loop
    until shutdown. Returns the runner's exit code.

    Alongside that loop the process runs the stage's admission loop, which creates
    the work-units the stage's upstream state says should exist. It admits nothing
    unless automatic admission is switched on for this stage, and it is stopped
    and joined before the worker returns.
    """
    admission_config = admission_config if admission_config is not None else AdmissionConfig()
    engine = get_engine(DatabaseConfig().database_url)
    llm_client = build_llm_client(contextualization_config)
    markdown_source = S3MarkdownSource(engine, S3Client(conversion_config))

    handler = ContextualizationStageHandler(
        engine,
        llm_client,
        markdown_source,
        ConversionOutputFreshness(),
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
    admission = AdmissionLoop(
        engine,
        contextualization_adapter(admission_config),
        interval_seconds=admission_config.admission_interval_seconds,
    )
    with admission:
        return runner.run()
