"""Summarize a document under a summary run."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import UUID

from sqlmodel import Session, select

from aizk.ai.providers.llm import ModelConfig
from aizk.contextualization.core.applicability import record_summary_run_applicability
from aizk.contextualization.core.keys import (
    MAX_SUMMARY_CHARS,
    SUMMARY_INSTRUCTIONS,
    SUMMARY_LENGTH_GUIDANCE_TEMPLATE,
    SUMMARY_PROMPT_DATA_POLICY,
    SUMMARY_PROMPT_HASH,
    SUMMARY_PROMPT_KIND,
    SUMMARY_SOFT_LIMIT_CHARS,
    _prompt_json,
    summary_config_fingerprint,
    summary_derivation_key,
    summary_input_fingerprint,
)
from aizk.graph._version import SUMMARY_VERSION
from aizk.graph.chunks import memo_get, memo_upsert_and_read
from aizk.graph.datamodel import MEMO_KIND_SUMMARY, DocumentSummary
from aizk.graph.variants import SUMMARY_STAGE, StalePlanError, _active_run
from aizk.pipeline.identity import DERIVATION_KEY_FORMAT
from aizk.pipeline.run import record_run
from aizk.utilities.hashing import content_fingerprint

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from aizk.ai.providers.llm import LLMClient

logger = logging.getLogger(__name__)


def _summary_prompt(document_text: str) -> str:
    """Build a summary prompt with length guidance.

    Only the core instructions contribute to ``SUMMARY_PROMPT_HASH``.
    """
    instructions = (
        f"{SUMMARY_INSTRUCTIONS} {SUMMARY_LENGTH_GUIDANCE_TEMPLATE.format(soft_limit=SUMMARY_SOFT_LIMIT_CHARS)}"
    )
    return (
        f"{SUMMARY_PROMPT_KIND}\n"
        f"{SUMMARY_PROMPT_DATA_POLICY}\n"
        f"{_prompt_json({'instructions': instructions, 'document': document_text})}"
    )


def _validate_summary_text(summary_text: str) -> str:
    """Validate and normalize a model-produced summary.

    Returns the stripped text.

    Raises:
        ValueError: If the normalized summary exceeds ``MAX_SUMMARY_CHARS``.
    """
    normalized = summary_text.strip()
    if len(normalized) > MAX_SUMMARY_CHARS:
        raise ValueError(f"summary is too long: {len(normalized)} chars exceeds {MAX_SUMMARY_CHARS} char limit")
    return normalized


def generate_summary_text(client: "LLMClient", document_text: str) -> str:
    """Generate unvalidated summary text without database access.

    Length validation occurs when the summary is persisted.
    """
    return client.generate(_summary_prompt(document_text))


def resolve_summary_text(
    engine: "Engine",
    client: "LLMClient",
    *,
    source_id: UUID,
    document_text: str,
    summary_version: int = SUMMARY_VERSION,
    model_config: ModelConfig,
) -> str:
    """Return the summary for a derivation key, reusing an active run or memo when possible.

    The function makes at most one model call. On a miss, it validates the output
    and stores it in a short autonomous transaction; it does not hold a
    transaction across model I/O.

    Args:
        engine: Engine used for reads and memo persistence.
        client: Model client used on a miss.
        source_id: Source scope for the memo.
        document_text: Document content used for the summary and its key.
        summary_version: Summary behavior version.
        model_config: Resolved model identity in the key.

    Returns:
        The summary text to persist.
    """
    key = summary_derivation_key(document_text, summary_version, model_config)
    with Session(engine) as read:
        active = _active_run(read, SUMMARY_STAGE, source_id)
        if active is not None and active.derivation_key == key:
            existing = read.exec(select(DocumentSummary).where(DocumentSummary.run_id == active.id)).first()
            if existing is not None:
                return existing.summary_text
        memoed = memo_get(read, MEMO_KIND_SUMMARY, source_id, key)
    if memoed is not None:
        return memoed
    validated = _validate_summary_text(generate_summary_text(client, document_text))
    return memo_upsert_and_read(engine, MEMO_KIND_SUMMARY, source_id, key, validated)


def summarize_document(
    session: "Session",
    client: "LLMClient",
    *,
    source_id: UUID,
    conversion_output_id: str,
    markdown_hash_xx64: str,
    document_text: str,
    summary_version: int = SUMMARY_VERSION,
    model_config: ModelConfig,
    precomputed_summary_text: str | None = None,
) -> DocumentSummary:
    """Produce or reuse the active summary for a document.

    An active run with the same derivation key is reused without a model call.
    Otherwise the function stages a new run and summary, using the supplied text
    or one model call. A different conversion-output locator adds an applicability
    row. The locator and Markdown hash are recorded as provenance but do not enter
    the derivation key.

    The function does not commit. It flushes the returned row before returning.

    Raises:
        StalePlanError: If supplied summary text conflicts with the active matching
            run.
    """
    key = summary_derivation_key(document_text, summary_version, model_config)
    active = _active_run(session, SUMMARY_STAGE, source_id)
    if active is not None and active.derivation_key == key:
        existing = session.exec(select(DocumentSummary).where(DocumentSummary.run_id == active.id)).first()
        if existing is not None:
            # The revisions in hand were conditioned on ``precomputed_summary_text``.
            # If the active summary's text differs (an overlapping attempt re-summarized
            # under the same key between plan and apply), reusing it would record a
            # different summary as provenance than the one the revisions used. Fail
            # retryably so the unit re-resolves and regenerates against the current
            # summary rather than persisting mismatched provenance.
            if precomputed_summary_text is not None and existing.summary_text != precomputed_summary_text:
                raise StalePlanError(
                    f"active summary for source {source_id!r} changed since the revisions were planned; "
                    "retry to re-resolve and regenerate against the current summary"
                )
            if existing.conversion_output_id != conversion_output_id:
                # The active run is also current for a later artifact locator whose
                # content is unchanged. The writer is idempotent on the primary key.
                record_summary_run_applicability(
                    session,
                    run_id=active.id,
                    conversion_output_id=conversion_output_id,
                    input_fingerprint=summary_input_fingerprint(document_text),
                    model_config=model_config,
                    summary_version=summary_version,
                )
            logger.debug("Reusing active summary run id=%s for source=%s", active.id, source_id)
            return existing

    raw_summary = (
        precomputed_summary_text
        if precomputed_summary_text is not None
        else generate_summary_text(client, document_text)
    )
    summary_text = _validate_summary_text(raw_summary)
    run = record_run(
        session,
        stage=SUMMARY_STAGE,
        scope_id=source_id,
        derivation_key=key,
        version_stamps={
            "model_provider": model_config.provider,
            "model_model": model_config.model,
            "summary_prompt_hash": SUMMARY_PROMPT_HASH,
            "summary_version": str(summary_version),
            "input_fingerprint": summary_input_fingerprint(document_text),
            "config_fingerprint": summary_config_fingerprint(model_config, summary_version),
            "derivation_key_format": DERIVATION_KEY_FORMAT,
        },
    )
    summary = DocumentSummary(
        run_id=run.id,
        conversion_output_id=conversion_output_id,
        summary_text=summary_text,
        markdown_hash_xx64=markdown_hash_xx64,
        summary_version=summary_version,
        summary_text_fingerprint=content_fingerprint(summary_text),
    )
    session.add(summary)
    session.flush()
    logger.debug("Recorded summary run id=%s summary id=%s for source=%s", run.id, summary.id, source_id)
    return summary
