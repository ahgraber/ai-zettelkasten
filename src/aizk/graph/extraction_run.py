"""The per-source extraction unit of work and its two entry points.

This module is the single write path both run modes go through for one
source's extraction: :func:`extract_document` resolves a source's chunks,
extracts their mentions, and persists them under one extraction run, in three
phases:

1. **Read** (an ordinary, short-lived session) — resolve the source's active
   chunking run and its chunks in document order, and the consumed upstream
   run's derivation key.
2. **Extract** (pure, no DB writes) — select each chunk's input text per
   ``input_policy`` and run the injected :class:`~aizk.graph.extraction.EntityExtractor`
   over it via :func:`~aizk.graph.extraction.extract_chunk_mentions`. Model
   calls happen only here, never inside the write transaction.
3. **Write** (one short ``BEGIN IMMEDIATE`` transaction) — open (or reuse) the
   source's extraction run and persist every chunk's drafts, so the whole
   document's run and mention/co-occurrence rows commit atomically: a mid-persist
   failure rolls back everything, leaving no newly-active run and no readable
   mentions from the failed attempt.

:func:`extract_source` (incremental) and :func:`extract_corpus` (bulk/backfill)
are thin entry points that both delegate to :func:`extract_document`, so
run-mode independence — the same inputs and versions yield the same mentions and
co-occurrences regardless of mode — holds by construction rather than by two
write paths staying in sync. Neither entry point does any scheduling,
throttling, or concurrency control; that is the caller's/runtime's concern.

:func:`stale_extraction_sources` is the stage's staleness derivation: which
sources' active extraction runs consumed since-superseded upstream state. It
resolves the current upstream key through the same resolver the write path uses,
so a stale verdict and what a re-extraction would consume cannot disagree.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from typing import TYPE_CHECKING

from sqlmodel import Session, select

from aizk.graph.contextualization import VARIANT_STAGE
from aizk.graph.datamodel import INPUT_KIND_RAW
from aizk.graph.db import begin_immediate
from aizk.graph.extraction import (
    MATERIALIZER_VERSION,
    EntityExtractor,
    ExtractionInput,
    extract_chunk_mentions,
    select_extraction_input,
)
from aizk.graph.mention_store import (
    INPUT_POLICY_CONTEXTUALIZED,
    INPUT_POLICY_RAW,
    MENTION_EXTRACTION_STAGE,
    InputPolicy,
    MentionDraft,
    open_extraction_run,
    persist_mentions,
)
from aizk.graph.persistence import active_chunking_run, document_order_chunks
from aizk.pipeline.run import PipelineRun, RunStatus

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy import Engine

    from aizk.graph.datamodel import Chunk

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DocumentExtractionResult:
    """The extraction run :func:`extract_document` produced (or reused) for one source.

    Carries the run id and counts as primitives (not the ORM
    :class:`~aizk.pipeline.run.PipelineRun`, which detaches once the write
    transaction commits) so the caller can log or assert without re-querying.
    """

    run_id: int
    source_id: str
    chunk_count: int
    mention_count: int


@dataclass(frozen=True)
class _ChunkWorkItem:
    """One chunk's extraction inputs, materialized into plain values inside the read session.

    The extract phase runs after the read session closes, so it must never
    touch ORM instances (whose attribute access can require a live session);
    this snapshot carries exactly the per-chunk facts
    :func:`~aizk.graph.extraction.extract_chunk_mentions` consumes.
    """

    chunk_id: str
    raw_text: str
    extraction_input: ExtractionInput


def _resolve_upstream_derivation_key(
    session: "Session",
    *,
    source_id: str,
    input_policy: InputPolicy,
    chunking_run: PipelineRun,
) -> str:
    """Resolve the consumed upstream run's derivation key for a source's extraction.

    Under ``input_policy = "raw"`` extraction consumes the chunking generation
    unconditionally, so this is always the active chunking run's key. Under
    ``input_policy = "contextualized"`` it is the source's active
    :data:`~aizk.graph.contextualization.VARIANT_STAGE` run's key when one
    exists; a source with no contextualization run at all falls back to the
    chunking run's key — the consumed upstream is then the chunking generation
    itself, so when a contextualization run later appears for the source, the
    upstream key changes and the next extraction opens a superseding run.
    """
    if input_policy == INPUT_POLICY_CONTEXTUALIZED:
        variant_run = session.exec(
            select(PipelineRun).where(
                PipelineRun.stage == VARIANT_STAGE,
                PipelineRun.scope_id == source_id,
                PipelineRun.status == RunStatus.ACTIVE,
            )
        ).one_or_none()
        if variant_run is not None:
            return variant_run.derivation_key
    return chunking_run.derivation_key


def _recorded_upstream(run: PipelineRun) -> tuple[str, InputPolicy] | None:
    """Return the upstream key and input policy an extraction run recorded, or ``None``.

    The upstream key is one of the four components of the run's canonical
    ``derivation_key``; the input policy is one of its version stamps. A run whose
    records cannot be read is reported as unresolvable rather than guessed at, so
    an unreadable record never becomes a spend decision.
    """
    try:
        upstream_key = json.loads(run.derivation_key)["upstream_derivation_key"]
        input_policy = json.loads(run.version_stamps_json)["input_policy"]
    except (TypeError, ValueError, KeyError):
        logger.warning(
            "Extraction run %s does not carry a readable upstream record; treating its source as current",
            run.id,
            extra={"run_id": run.id, "scope_id": run.scope_id},
        )
        return None
    if input_policy not in (INPUT_POLICY_CONTEXTUALIZED, INPUT_POLICY_RAW):
        logger.warning(
            "Extraction run %s recorded input policy %r, which is not a legal policy",
            run.id,
            input_policy,
            extra={"run_id": run.id, "scope_id": run.scope_id},
        )
        return None
    return upstream_key, input_policy


def stale_extraction_sources(session: "Session") -> set[str]:
    """Return the scope ids whose active extraction run consumed since-superseded upstream state.

    A source is stale when the upstream derivation key its active extraction run
    recorded differs from the key its current active runs would yield: re-extracting
    would read a different generation than the one already extracted. A re-chunk
    makes a source stale; so does a contextualization run appearing for a source
    whose extraction fell back to raw chunk text because no variants existed yet.

    Resolution goes through :func:`_resolve_upstream_derivation_key`, the same
    resolver :func:`extract_document` uses at execute time.

    The comparison uses the policy the run itself recorded, so the verdict is
    about upstream supersession alone. A change to the configured input policy is
    a different kind of invalidation: it changes the extraction derivation key
    directly, and the next run supersedes on that basis without being called stale.

    Staleness never makes a source pending — no staleness condition admits work
    automatically. It marks work an operator may choose to re-admit.

    Args:
        session: Active, read-only session.

    Returns:
        The stale sources' ``scope_id`` values (``str(source_id)``).
    """
    stale: set[str] = set()
    active_runs = session.exec(
        select(PipelineRun).where(
            PipelineRun.stage == MENTION_EXTRACTION_STAGE,
            PipelineRun.status == RunStatus.ACTIVE,
        )
    ).all()
    for run in active_runs:
        recorded = _recorded_upstream(run)
        if recorded is None:
            continue
        recorded_key, input_policy = recorded
        chunking_run = active_chunking_run(session, run.scope_id)
        if chunking_run is None:
            # Nothing active to re-extract from, so there is no different generation
            # to be behind; a source in this state is not re-admittable work.
            continue
        current_key = _resolve_upstream_derivation_key(
            session, source_id=run.scope_id, input_policy=input_policy, chunking_run=chunking_run
        )
        if current_key != recorded_key:
            stale.add(run.scope_id)
    return stale


def _resolve_extraction_input(session: "Session", chunk: "Chunk", *, input_policy: InputPolicy) -> ExtractionInput:
    """Resolve the text one chunk is read from, honoring the run-level ``input_policy``.

    ``raw`` is a run-level policy, not a per-chunk fact: every chunk is read
    from its own raw text regardless of whether a contextualized variant
    exists, so this short-circuits
    :func:`~aizk.graph.extraction.select_extraction_input` entirely rather than
    consulting it and discarding a contextualized result. ``contextualized``
    delegates to it for the active-variant-else-raw-fallback resolution.
    """
    if input_policy == INPUT_POLICY_RAW:
        return ExtractionInput(text=chunk.text, input_kind=INPUT_KIND_RAW)
    return select_extraction_input(session, chunk)


def extract_document(
    engine: "Engine",
    *,
    source_id: str,
    extractor: EntityExtractor,
    input_policy: InputPolicy,
) -> DocumentExtractionResult:
    """Extract and persist one source's mentions, in three phases with one short write transaction.

    **Read** (ordinary session, no writer lock): resolves the source's active
    chunking run — raising if none, since an unchunked source has nothing to
    extract — its chunks in document order, and the consumed upstream run's
    derivation key (see :func:`_resolve_upstream_derivation_key`).

    **Extract** (pure, no DB writes): per chunk, resolves the input text per
    ``input_policy`` and invokes ``extractor`` via
    :func:`~aizk.graph.extraction.extract_chunk_mentions`. Model calls happen
    only here, never inside the write transaction that follows, so latency
    never holds the single serialized writer's lock.

    **Write** (one short ``BEGIN IMMEDIATE`` transaction for the whole
    document): opens (or reuses) the source's extraction run and persists every
    chunk's drafts in one call, so the run record and all of its mention and
    co-occurrence rows commit atomically. A mid-persist failure rolls back the
    whole transaction — no newly-active run, no readable mentions from the
    failed attempt, and the prior active run (if any) remains active. A retry
    re-enters through the unchanged derivation key
    (:func:`~aizk.pipeline.run.reuse_or_record_run`) and idempotent inserts, so
    it reuses the run and does not duplicate mentions.

    Args:
        engine: The shared engine; the read phase and the write phase each open
            their own short session/transaction off it.
        source_id: The durable source identity; the extraction run's scope.
        extractor: The single injected NER access point (see
            :class:`~aizk.graph.extraction.EntityExtractor`).
        input_policy: The raw-vs-contextualized input toggle (see
            :data:`~aizk.graph.mention_store.InputPolicy`).

    Returns:
        A :class:`DocumentExtractionResult` with the active run's id and counts.

    Raises:
        ValueError: If ``input_policy`` is not one of
            :data:`~aizk.graph.mention_store.InputPolicy`'s legal values —
            rejected here before any read or model invocation (the read-phase
            helpers branch on different policy sentinels, so an out-of-domain
            value must never reach them), and independently by
            :func:`~aizk.graph.mention_store.open_extraction_run` at the write
            boundary; or if the source has no active chunking run.
    """
    if input_policy not in (INPUT_POLICY_CONTEXTUALIZED, INPUT_POLICY_RAW):
        raise ValueError(
            f"input_policy {input_policy!r} is not one of "
            f"{sorted((INPUT_POLICY_CONTEXTUALIZED, INPUT_POLICY_RAW))}; "
            "rejected before any extraction work runs"
        )

    with Session(engine) as read_session:
        chunking_run = active_chunking_run(read_session, source_id)
        if chunking_run is None or chunking_run.id is None:
            raise ValueError(
                f"source {source_id!r} has no active chunking run; an unchunked source has nothing to extract"
            )
        # Materialize each chunk's work item into plain values while the read
        # session is still open, so the extract phase below never touches ORM
        # instances detached from a closed session.
        work_items = [
            _ChunkWorkItem(
                chunk_id=chunk.chunk_id,
                raw_text=chunk.text,
                extraction_input=_resolve_extraction_input(read_session, chunk, input_policy=input_policy),
            )
            for chunk, _, _ in document_order_chunks(read_session, chunking_run.id)
        ]
        upstream_derivation_key = _resolve_upstream_derivation_key(
            read_session, source_id=source_id, input_policy=input_policy, chunking_run=chunking_run
        )

    drafts: list[MentionDraft] = []
    for item in work_items:
        drafts.extend(
            extract_chunk_mentions(
                chunk_id=item.chunk_id,
                raw_chunk_text=item.raw_text,
                extraction_input=item.extraction_input,
                extractor=extractor,
            )
        )

    with begin_immediate(engine) as session:
        run = open_extraction_run(
            session,
            source_id=source_id,
            extractor_version=extractor.extractor_version,
            materializer_version=MATERIALIZER_VERSION,
            input_policy=input_policy,
            upstream_derivation_key=upstream_derivation_key,
        )
        persisted = persist_mentions(session, run=run, mentions=drafts)
        assert run.id is not None  # noqa: S101 — a persisted run always carries an id
        result = DocumentExtractionResult(
            run_id=run.id,
            source_id=source_id,
            chunk_count=len(work_items),
            mention_count=len(persisted),
        )

    logger.debug(
        "Extracted source=%s run id=%s: %d chunks, %d mentions",
        source_id,
        result.run_id,
        result.chunk_count,
        result.mention_count,
    )
    return result


def extract_source(
    engine: "Engine",
    *,
    source_id: str,
    extractor: EntityExtractor,
    input_policy: InputPolicy,
) -> DocumentExtractionResult:
    """Extract one source incrementally, through the same per-document unit :func:`extract_corpus` uses.

    The incremental entry point: one call processes one source. It adds no
    logic beyond naming the enqueue pattern — both this and :func:`extract_corpus`
    delegate to :func:`extract_document`, so the produced run and mention records
    are identical regardless of which entry point drove them.
    """
    return extract_document(engine, source_id=source_id, extractor=extractor, input_policy=input_policy)


def extract_corpus(
    engine: "Engine",
    *,
    source_ids: "Sequence[str]",
    extractor: EntityExtractor,
    input_policy: InputPolicy,
) -> list[DocumentExtractionResult]:
    """Extract many sources in bulk/backfill mode, one document transaction at a time.

    Iterates :func:`extract_document` per source — the same per-document unit
    :func:`extract_source` uses — so a batch run produces exactly the records an
    equivalent sequence of incremental calls would. Throttling, scheduling, and
    concurrency are the caller's/runtime's concern; this function only sequences
    the per-document calls.

    Failure semantics: each document commits its own transaction, so a
    mid-corpus failure propagates immediately while every already-processed
    source's run and mentions stay committed. The return value is
    all-or-nothing — a raised failure yields no result list — so it cannot tell
    the caller which sources committed; recover that from the store
    (:func:`~aizk.graph.mention_store.active_extraction_run` per source), and
    re-invoking with the same inputs is safe because each per-document unit is
    idempotent on its derivation key.

    Args:
        engine: The shared engine, passed through to each :func:`extract_document`
            call.
        source_ids: The sources to extract, processed in the given order.
        extractor: The single injected NER access point, shared across sources.
        input_policy: The raw-vs-contextualized input toggle, shared across
            sources.

    Returns:
        One :class:`DocumentExtractionResult` per source, in ``source_ids`` order.
    """
    return [
        extract_document(engine, source_id=source_id, extractor=extractor, input_policy=input_policy)
        for source_id in source_ids
    ]
