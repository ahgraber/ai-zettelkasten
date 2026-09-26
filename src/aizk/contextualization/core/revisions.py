"""Contextualize a document's chunks as revisions under a variant run."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING
from uuid import UUID

from sqlmodel import Session, select

from aizk.ai.providers.llm import ModelConfig
from aizk.contextualization.core.applicability import record_contextualization_run_applicability
from aizk.contextualization.core.keys import (
    CONTEXT_PROMPT_DATA_POLICY,
    CONTEXT_PROMPT_HASH,
    CONTEXT_PROMPT_KIND,
    CONTEXT_WINDOW_POLICY,
    CONTEXTUALIZATION_INSTRUCTIONS,
    MAX_REVISION_CHUNK_RATIO,
    MIN_REVISION_LIMIT_CHARS,
    _prompt_json,
    contextualization_config_fingerprint,
    contextualization_row_derivation_key,
    contextualization_run_derivation_key,
    contextualization_run_input_fingerprint,
)
from aizk.graph._version import CONTEXT_VERSION
from aizk.graph.chunks import CHUNKING_STAGE, manifest_of_run, memo_get, memo_upsert_and_read
from aizk.graph.content_index import index_contextualized_content
from aizk.graph.datamodel import (
    INPUT_KIND_CONTEXTUALIZED,
    INPUT_KIND_RAW,
    MEMO_KIND_REVISION,
    ContextualizationRunInput,
    ContextualizedChunk,
    DocumentSummary,
)
from aizk.graph.fingerprints import PayloadItem, chunk_key, payload_fingerprint
from aizk.graph.variants import VARIANT_STAGE, StalePlanError, _active_run
from aizk.pipeline.identity import DERIVATION_KEY_FORMAT
from aizk.pipeline.run import PipelineRun, record_run
from aizk.utilities.hashing import content_fingerprint

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy import Engine

    from aizk.ai.providers.llm import LLMClient
    from aizk.chunking import Chunk as SplitterChunk

logger = logging.getLogger(__name__)


def _contextualization_framing(
    summary_text: str,
    prior_2: "SplitterChunk | None",
    prior_1: "SplitterChunk | None",
    working: "SplitterChunk",
    next_1: "SplitterChunk | None",
) -> str:
    """Build the safe 2-prior / 1-next framing for one chunk."""
    payload = {
        "instructions": CONTEXTUALIZATION_INSTRUCTIONS,
        "context_window_policy": CONTEXT_WINDOW_POLICY,
        "summary": summary_text,
        "prior_chunk_2": prior_2.text if prior_2 is not None else "",
        "prior_chunk_1": prior_1.text if prior_1 is not None else "",
        "working_chunk": working.text,
        "next_chunk_1": next_1.text if next_1 is not None else "",
    }
    return f"{CONTEXT_PROMPT_KIND}\n{CONTEXT_PROMPT_DATA_POLICY}\n{_prompt_json(payload)}"


def _revision_limit(working_text: str) -> int:
    """Return the maximum accepted contextualized-revision length for a working chunk."""
    ratio_limit = int(len(working_text) * MAX_REVISION_CHUNK_RATIO)
    return max(MIN_REVISION_LIMIT_CHARS, ratio_limit)


def _validate_contextualized_text(working_text: str, contextualized_text: str) -> str:
    """Validate and normalize a model-produced contextualized revision.

    An empty result means the raw chunk is already self-contained. A non-empty
    revision must fit the chunk-relative length limit.
    """
    normalized = contextualized_text.strip()
    if normalized == "":
        return ""
    limit = _revision_limit(working_text)
    if len(normalized) > limit:
        raise ValueError(f"contextualized text is too long: {len(normalized)} chars exceeds {limit} char limit")
    return normalized


def _verify_chunking_provenance(
    session: "Session",
    source_id: UUID,
    chunking_run_id: int,
    splitter_version: int,
    chunks: "Sequence[SplitterChunk]",
) -> None:
    """Ensure that ``chunks`` belong to ``chunking_run_id`` before recording provenance.

    The run must be a chunking run for ``source_id`` with the supplied
    ``splitter_version``, and its manifest must contain each chunk at its span.

    Raises:
        ValueError: If any provenance check fails.
    """
    run = session.get(PipelineRun, chunking_run_id)
    if run is None or run.stage != CHUNKING_STAGE:
        raise ValueError(f"chunking run {chunking_run_id!r} is missing or is not a chunking run")
    if run.scope_id != source_id:
        raise ValueError(f"chunking run {chunking_run_id!r} belongs to source {run.scope_id!r}, not {source_id!r}")
    recorded_version = json.loads(run.version_stamps_json).get("splitter_version")
    if recorded_version != str(splitter_version):
        raise ValueError(
            f"splitter_version {splitter_version} does not match chunking run {chunking_run_id!r} "
            f"(recorded {recorded_version})"
        )
    manifest = {(m.chunk_id, m.span_start, m.span_end) for m in manifest_of_run(session, chunking_run_id)}
    absent = [
        c.chunk_id for c in chunks if c.chunk_id is None or (UUID(c.chunk_id), c.span[0], c.span[1]) not in manifest
    ]
    if absent:
        raise ValueError(
            f"chunks {absent} are not in chunking run {chunking_run_id!r}'s manifest at their supplied span"
        )


def _generate_one_revision(
    client: "LLMClient",
    summary_text: str,
    prior_2: "SplitterChunk | None",
    prior_1: "SplitterChunk | None",
    working: "SplitterChunk",
    next_1: "SplitterChunk | None",
) -> str:
    """Generate one raw contextualization revision without database access.

    Length validation occurs when the revision is memoized or persisted.
    """
    return client.generate(_contextualization_framing(summary_text, prior_2, prior_1, working, next_1))


def generate_revisions(
    client: "LLMClient",
    summary_text: str,
    ordered_chunks: "Sequence[SplitterChunk]",
) -> list[str]:
    """Generate raw revisions in document order.

    Each revision uses the chunk's 2-prior/1-next window. Length validation occurs
    at persistence.
    """
    ordered = list(ordered_chunks)
    revisions: list[str] = []
    for index, chunk in enumerate(ordered):
        prior_2 = ordered[index - 2] if index > 1 else None
        prior_1 = ordered[index - 1] if index > 0 else None
        next_1 = ordered[index + 1] if index < len(ordered) - 1 else None
        revisions.append(_generate_one_revision(client, summary_text, prior_2, prior_1, chunk, next_1))
    return revisions


def resolve_revisions(
    engine: "Engine",
    client: "LLMClient",
    *,
    source_id: UUID,
    summary_text: str,
    ordered_chunks: "Sequence[SplitterChunk]",
    splitter_version: int,
    context_version: int = CONTEXT_VERSION,
    model_config: ModelConfig,
) -> list[str] | None:
    """Return revisions, reusing a complete active run or retained memo entries.

    The function makes at most one model call per missing chunk. Each miss validates
    and stores its output in an autonomous transaction; no transaction spans model
    I/O.

    Returns:
        ``None`` when a complete active variant run is reused. Otherwise one raw or
        empty revision per chunk in document order.
    """
    ordered = list(ordered_chunks)
    run_key = contextualization_run_derivation_key(
        summary_text, ordered, splitter_version, context_version, model_config
    )
    with Session(engine) as read:
        active = _active_run(read, VARIANT_STAGE, source_id)
        if active is not None and active.derivation_key == run_key:
            count = len(read.exec(select(ContextualizedChunk).where(ContextualizedChunk.run_id == active.id)).all())
            if count == len(ordered):
                return None

    revisions: list[str] = []
    for index, chunk in enumerate(ordered):
        prior_2 = ordered[index - 2] if index > 1 else None
        prior_1 = ordered[index - 1] if index > 0 else None
        next_1 = ordered[index + 1] if index < len(ordered) - 1 else None
        row_key = contextualization_row_derivation_key(
            summary_text, chunk, prior_2, prior_1, next_1, splitter_version, context_version, model_config
        )
        with Session(engine) as read:
            memoed = memo_get(read, MEMO_KIND_REVISION, source_id, row_key)
        if memoed is not None:
            revisions.append(memoed)
            continue
        raw = _generate_one_revision(client, summary_text, prior_2, prior_1, chunk, next_1)
        validated = _validate_contextualized_text(chunk.text, raw)
        revisions.append(memo_upsert_and_read(engine, MEMO_KIND_REVISION, source_id, row_key, validated))
    return revisions


def assert_variant_matches_run_input(run_input: ContextualizationRunInput, variant: ContextualizedChunk) -> None:
    """Validate a variant's run-level provenance pointers.

    Raises:
        ValueError: If either upstream run ID differs from the run input.
    """
    if variant.summary_run_id != run_input.summary_run_id or variant.chunking_run_id != run_input.chunking_run_id:
        raise ValueError(
            f"variant (summary_run_id={variant.summary_run_id!r}, chunking_run_id={variant.chunking_run_id!r}) "
            f"does not match run {run_input.run_id!r}'s recorded input "
            f"(summary_run_id={run_input.summary_run_id!r}, chunking_run_id={run_input.chunking_run_id!r})"
        )


def contextualize_chunks(
    session: "Session",
    client: "LLMClient",
    *,
    source_id: UUID,
    summary: DocumentSummary,
    chunks: "Sequence[SplitterChunk]",
    chunking_run_id: int,
    splitter_version: int,
    context_version: int = CONTEXT_VERSION,
    model_config: ModelConfig,
    precomputed_revisions: "Sequence[str] | None" = None,
    reuse_only: bool = False,
) -> list[ContextualizedChunk]:
    """Produce or reuse active contextualized variants for a source's chunks.

    ``chunks`` must be in document order. A complete active run with the same key
    is reused without model calls; otherwise the function stages one variant per
    chunk, including an empty revision when the raw chunk is already self-contained.
    ``precomputed_revisions`` supplies outputs generated outside the write lock.
    The source chunk rows are not modified.

    The function does not commit; the caller owns the transaction.

    Raises:
        StalePlanError: If ``reuse_only`` is set but the planned complete run is
            unavailable.
        ValueError: If the summary, chunks, or chunking-run provenance is invalid.
    """
    ordered = list(chunks)
    summary_run = session.get(PipelineRun, summary.run_id)
    if summary_run is None:
        raise ValueError(f"summary run {summary.run_id!r} is missing")
    if summary_run.scope_id != source_id:
        raise ValueError(f"summary belongs to source {summary_run.scope_id!r}, not {source_id!r}")
    # A chunk's source_id is the splitter's string form of the identity, so the
    # comparison converts; without it every chunk would read as foreign.
    foreign = [c.chunk_id for c in ordered if c.source_id != str(source_id)]
    if foreign:
        raise ValueError(f"chunks {foreign} do not belong to source {source_id!r}")
    _verify_chunking_provenance(session, source_id, chunking_run_id, splitter_version, ordered)
    run_derivation_key = contextualization_run_derivation_key(
        summary.summary_text, ordered, splitter_version, context_version, model_config
    )
    active = _active_run(session, VARIANT_STAGE, source_id)
    if active is not None and active.derivation_key == run_derivation_key:
        existing = list(
            session.exec(
                select(ContextualizedChunk)
                .where(ContextualizedChunk.run_id == active.id)
                .order_by(ContextualizedChunk.id)
            ).all()
        )
        # A matching active run already holds one variant per chunk (zero when the
        # document has no chunks); reuse it. A short count means a partial prior
        # write, so fall through and regenerate rather than reuse a torn run.
        if len(existing) == len(ordered):
            active_input = session.get(ContextualizationRunInput, active.id)
            if active_input is not None and (active_input.summary_run_id, active_input.chunking_run_id) != (
                summary.run_id,
                chunking_run_id,
            ):
                # The active run is also current for a regenerated upstream pair with
                # identical content. The writer is idempotent on the primary key.
                record_contextualization_run_applicability(
                    session,
                    run_id=active.id,
                    summary_run_id=summary.run_id,
                    chunking_run_id=chunking_run_id,
                    model_config=model_config,
                    splitter_version=splitter_version,
                    context_version=context_version,
                )
            logger.debug("Reusing active variant run id=%s for source=%s", active.id, source_id)
            return existing

    # A complete active run can be reused without carrying revisions. If it vanishes
    # or becomes incomplete before persistence, fail retryably instead of generating
    # under the write lock or treating an empty precomputed set as a torn run.
    if reuse_only:
        raise StalePlanError(
            f"variant run for source {source_id!r} planned for reuse is no longer active/complete; "
            "retry to regenerate the revisions outside the write lock"
        )

    # Raw revisions come from the caller (LLM run outside the write lock) or are
    # produced here; either way they are validated (cheap) before persistence.
    if precomputed_revisions is not None:
        raw_revisions = list(precomputed_revisions)
        if len(raw_revisions) != len(ordered):
            raise ValueError(
                f"precomputed_revisions has {len(raw_revisions)} entries, expected {len(ordered)} (one per chunk)"
            )
    else:
        raw_revisions = generate_revisions(client, summary.summary_text, ordered)

    generated: list[tuple[SplitterChunk, SplitterChunk | None, SplitterChunk | None, SplitterChunk | None, str]] = []
    for index, chunk in enumerate(ordered):
        prior_2 = ordered[index - 2] if index > 1 else None
        prior_1 = ordered[index - 1] if index > 0 else None
        next_1 = ordered[index + 1] if index < len(ordered) - 1 else None
        revision = _validate_contextualized_text(chunk.text, raw_revisions[index])
        generated.append((chunk, prior_2, prior_1, next_1, revision))

    config_fingerprint = contextualization_config_fingerprint(splitter_version, context_version, model_config)
    input_fingerprint = contextualization_run_input_fingerprint(summary.summary_text, [chunk_key(c) for c in ordered])
    run = record_run(
        session,
        stage=VARIANT_STAGE,
        scope_id=source_id,
        derivation_key=run_derivation_key,
        version_stamps={
            "context_prompt_hash": CONTEXT_PROMPT_HASH,
            "context_version": str(context_version),
            "context_window_policy": CONTEXT_WINDOW_POLICY,
            "model_provider": model_config.provider,
            "model_model": model_config.model,
            "splitter_version": str(splitter_version),
            "summary_derivation_key": summary_run.derivation_key,
            "input_fingerprint": input_fingerprint,
            "config_fingerprint": config_fingerprint,
            "derivation_key_format": DERIVATION_KEY_FORMAT,
        },
    )
    output_fingerprint = payload_fingerprint(
        [
            PayloadItem(
                chunk_key(chunk),
                INPUT_KIND_CONTEXTUALIZED if revision != "" else INPUT_KIND_RAW,
                content_fingerprint(revision if revision != "" else chunk.text),
            )
            for chunk, _, _, _, revision in generated
        ]
    )
    run_input_row = ContextualizationRunInput(
        run_id=run.id,
        summary_run_id=summary.run_id,
        chunking_run_id=chunking_run_id,
        input_fingerprint=input_fingerprint,
        config_fingerprint=config_fingerprint,
        output_fingerprint=output_fingerprint,
    )
    session.add(run_input_row)
    variants: list[ContextualizedChunk] = []
    for chunk, prior_2, prior_1, next_1, revision in generated:
        chunk_id = UUID(chunk.chunk_id)
        variant = ContextualizedChunk(
            run_id=run.id,
            summary_run_id=summary.run_id,
            chunking_run_id=chunking_run_id,
            chunk_id=chunk_id,
            context_version=context_version,
            contextualized_text=revision,
            derivation_key=contextualization_row_derivation_key(
                summary.summary_text,
                chunk,
                prior_2,
                prior_1,
                next_1,
                splitter_version,
                context_version,
                model_config,
            ),
        )
        assert_variant_matches_run_input(run_input_row, variant)
        session.add(variant)
        variants.append(variant)
        # Index the committed variant once. An empty revision means self-contained,
        # so its contextualized representation is the raw chunk text. Sourced only
        # here (the committed persist path), never the memo, so retained
        # intermediate outputs are never searchable.
        index_contextualized_content(
            session,
            text_=revision if revision != "" else chunk.text,
            chunk_id=chunk_id,
            run_id=run.id,
            scope_id=source_id,
        )

    session.flush()
    logger.debug("Recorded variant run id=%s with %d variants for source=%s", run.id, len(variants), source_id)
    return variants
