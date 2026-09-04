"""The per-source extraction unit of work, its currentness classification, and its two entry points.

This module is the single write path both run modes go through for one
source's extraction: :func:`extract_document` resolves a source's chunks,
extracts their mentions, and persists them under one extraction run, in three
phases:

1. **Read** (an ordinary, short-lived session) — resolve the source's active
   chunking run, its chunks in document order, the payload extraction would
   read per chunk, and the run whose output that payload equals (see
   :func:`_resolve_upstream_run`). The candidate derivation key
   (:func:`~aizk.graph.mention_store.extraction_derivation_key`) then tells
   whether the active extraction run already matches.
2. **Extract** (pure, no DB writes) — only when the candidate key does not
   match the active run: select each chunk's input text per ``input_policy``
   and run the injected :class:`~aizk.graph.extraction.EntityExtractor` over it
   via :func:`~aizk.graph.extraction.extract_chunk_mentions`. Model calls
   happen only here, never inside a write transaction.
3. **Write** (one short ``BEGIN IMMEDIATE`` transaction) — re-validates the
   plan against current state. A matching candidate key reconciles: records
   :class:`~aizk.graph.datamodel.ExtractionRunApplicability` when the resolved
   upstream run differs from what the active run recorded, and returns without
   invoking the extractor or touching mentions. Otherwise it opens (or reuses)
   the source's extraction run, records its
   :class:`~aizk.graph.datamodel.ExtractionRunInput` on a new run, and persists
   every chunk's drafts, so the run record and all of its mention/co-occurrence
   rows commit atomically. Either way, a plan invalidated between the read and
   write phases raises :class:`~aizk.graph.contextualization.StalePlanError` and
   commits nothing.

:func:`extract_source` (incremental) and :func:`extract_corpus` (bulk/backfill)
are thin entry points that both delegate to :func:`extract_document`, so
run-mode independence — the same inputs and versions yield the same mentions and
co-occurrences regardless of mode — holds by construction rather than by two
write paths staying in sync. Neither entry point does any scheduling,
throttling, or concurrency control; that is the caller's/runtime's concern.

:class:`Currentness` and :func:`classify_extraction_sources` /
:func:`classify_extraction_source` are the stage's currentness derivation, read
from persisted columns alone (no chunk or variant content is loaded).
:func:`stale_extraction_sources` is the ``STALE`` subset — the only class the
dashboard's stale count reports. :func:`record_extraction_run_applicability` is
the one writer of applicability rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import logging
from typing import TYPE_CHECKING
from uuid import UUID

from sqlmodel import Session, func, select

from aizk.graph.contextualization import VARIANT_STAGE, StalePlanError
from aizk.graph.datamodel import (
    INPUT_KIND_RAW,
    ChunkRunInput,
    ContextualizationRunInput,
    ExtractionRunApplicability,
    ExtractionRunInput,
    Mention,
)
from aizk.graph.db import begin_immediate
from aizk.graph.extraction import (
    MATERIALIZER_VERSION,
    EntityExtractor,
    ExtractionInput,
    extract_chunk_mentions,
    select_extraction_input,
)
from aizk.graph.fingerprints import PayloadItem, chunk_key, payload_fingerprint
from aizk.graph.mention_store import (
    INPUT_POLICY_CONTEXTUALIZED,
    INPUT_POLICY_RAW,
    MENTION_EXTRACTION_STAGE,
    InputPolicy,
    MentionDraft,
    ResolvedExtractionConfig,
    active_extraction_run,
    extraction_derivation_key,
    open_extraction_run,
    persist_mentions,
)
from aizk.graph.persistence import CHUNKING_STAGE, active_chunking_run, document_order_chunks
from aizk.pipeline.identity import content_fingerprint
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
    source_id: UUID
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

    chunk_id: UUID
    raw_text: str
    extraction_input: ExtractionInput
    #: The chunk's portable content key (:func:`~aizk.graph.fingerprints.chunk_key`),
    #: so the extract phase can build the run's input-payload fingerprint after
    #: the read session closes.
    chunk_key: str


class Currentness(str, Enum):
    """A source's active extraction run's freshness against currently-resolved state.

    ``STALE`` — the payload or configuration a re-extraction would consume
    differs from what the active run recorded; re-extracting supersedes it.
    ``NEEDS_RECONCILIATION`` — the payload is unchanged, but the active run
    recorded a since-superseded upstream run and no
    :class:`~aizk.graph.datamodel.ExtractionRunApplicability` row covers the
    currently-resolved one; a reconciliation pass records that applicability with
    no extractor calls. ``CURRENT`` — the active run's provenance already matches,
    directly or through an applicability row.
    """

    CURRENT = "current"
    NEEDS_RECONCILIATION = "needs_reconciliation"
    STALE = "stale"


def _resolve_upstream_run(
    session: "Session",
    *,
    source_id: UUID,
    input_policy: InputPolicy,
    chunking_run: PipelineRun,
) -> PipelineRun:
    """Resolve the run whose output a source's extraction consumes.

    Under ``input_policy = "raw"`` extraction consumes the chunking generation
    unconditionally, so this is always the active chunking run. Under
    ``input_policy = "contextualized"`` it is the source's active
    :data:`~aizk.graph.contextualization.VARIANT_STAGE` run when one exists, else
    the chunking run — so when a contextualization run later appears, the
    upstream output changes and the next extraction opens a superseding run.
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
            return variant_run
    return chunking_run


def _replan_upstream(session: "Session", *, source_id: UUID, input_policy: InputPolicy) -> PipelineRun:
    """Re-resolve the run whose output extraction currently consumes, inside the write transaction.

    Raises:
        StalePlanError: If the source no longer has an active chunking run —
            reported as a retryable stale plan rather than an unrelated crash.
    """
    chunking_run = active_chunking_run(session, source_id)
    if chunking_run is None or chunking_run.id is None:
        raise StalePlanError(
            f"source {source_id!r} has no active chunking run at persist; "
            "retry to re-resolve and re-extract against current state"
        )
    return _resolve_upstream_run(session, source_id=source_id, input_policy=input_policy, chunking_run=chunking_run)


def _upstream_output_fingerprint(session: "Session", upstream_run: "PipelineRun") -> str:
    """Return the persisted output fingerprint of an upstream chunking or contextualization run.

    Reads the immutable provenance row the producing stage wrote —
    :attr:`~aizk.graph.datamodel.ChunkRunInput.chunk_set_fingerprint` for a
    chunking run, :attr:`~aizk.graph.datamodel.ContextualizationRunInput.output_fingerprint`
    for a contextualization run — never recomputed from content.

    Raises:
        ValueError: If ``upstream_run``'s stage is neither, or its provenance row
            is missing (that persist is atomic with the run, so a missing row is
            a programming error, not a race).
    """
    assert upstream_run.id is not None  # noqa: S101 — an active run is always persisted
    if upstream_run.stage == CHUNKING_STAGE:
        chunk_input = session.get(ChunkRunInput, upstream_run.id)
        if chunk_input is None:
            raise ValueError(f"chunking run {upstream_run.id!r} has no recorded ChunkRunInput")
        return chunk_input.chunk_set_fingerprint
    if upstream_run.stage == VARIANT_STAGE:
        context_input = session.get(ContextualizationRunInput, upstream_run.id)
        if context_input is None:
            raise ValueError(f"contextualization run {upstream_run.id!r} has no recorded ContextualizationRunInput")
        return context_input.output_fingerprint
    raise ValueError(
        f"run {upstream_run.id!r} has stage {upstream_run.stage!r}, expected a chunking or contextualization run"
    )


def _classify_extraction(
    session: "Session", *, resolved: "ResolvedExtractionConfig", scope_ids: "Sequence[UUID] | None" = None
) -> dict[UUID, "Currentness"]:
    """Classify sources' active extraction runs against currently-resolved upstream and configuration state.

    Issues a fixed number of statements regardless of how many sources are
    classified, comparing persisted fingerprint columns in Python. Loads no chunk
    or variant text.

    ``scope_ids`` narrows the corpus without changing the query shape, so
    :func:`classify_extraction_sources` and :func:`classify_extraction_source`
    share this body and cannot disagree.
    """
    config_fingerprint = resolved.config_fingerprint()

    extraction_query = (
        select(PipelineRun, ExtractionRunInput)
        .join(ExtractionRunInput, ExtractionRunInput.run_id == PipelineRun.id)
        .where(PipelineRun.stage == MENTION_EXTRACTION_STAGE, PipelineRun.status == RunStatus.ACTIVE)
    )
    if scope_ids is not None:
        extraction_query = extraction_query.where(PipelineRun.scope_id.in_(scope_ids))
    extraction_rows = session.exec(extraction_query).all()
    if not extraction_rows:
        return {}

    extraction_scope_ids = [run.scope_id for run, _ in extraction_rows]

    chunking_rows = session.exec(
        select(PipelineRun.id, PipelineRun.scope_id, ChunkRunInput.chunk_set_fingerprint)
        .join(ChunkRunInput, ChunkRunInput.run_id == PipelineRun.id)
        .where(
            PipelineRun.stage == CHUNKING_STAGE,
            PipelineRun.status == RunStatus.ACTIVE,
            PipelineRun.scope_id.in_(extraction_scope_ids),
        )
    ).all()
    chunking_upstream: dict[UUID, tuple[int, str]] = {
        scope_id: (run_id, output_fingerprint) for run_id, scope_id, output_fingerprint in chunking_rows
    }

    variant_upstream: dict[UUID, tuple[int, str]] = {}
    if resolved.input_policy == INPUT_POLICY_CONTEXTUALIZED:
        variant_rows = session.exec(
            select(PipelineRun.id, PipelineRun.scope_id, ContextualizationRunInput.output_fingerprint)
            .join(ContextualizationRunInput, ContextualizationRunInput.run_id == PipelineRun.id)
            .where(
                PipelineRun.stage == VARIANT_STAGE,
                PipelineRun.status == RunStatus.ACTIVE,
                PipelineRun.scope_id.in_(extraction_scope_ids),
            )
        ).all()
        variant_upstream = {
            scope_id: (run_id, output_fingerprint) for run_id, scope_id, output_fingerprint in variant_rows
        }

    extraction_run_ids = [run.id for run, _ in extraction_rows]
    applicability_rows = session.exec(
        select(ExtractionRunApplicability.run_id, ExtractionRunApplicability.upstream_run_id).where(
            ExtractionRunApplicability.run_id.in_(extraction_run_ids)
        )
    ).all()
    applicable_upstreams: dict[int, set[int]] = {}
    for run_id, upstream_run_id in applicability_rows:
        applicable_upstreams.setdefault(run_id, set()).add(upstream_run_id)

    result: dict[UUID, Currentness] = {}
    for run, run_input in extraction_rows:
        upstream = variant_upstream.get(run.scope_id) or chunking_upstream.get(run.scope_id)
        if upstream is None:
            # No active chunking run for the source: nothing to classify against.
            continue
        upstream_run_id, upstream_output_fingerprint = upstream
        if (
            upstream_output_fingerprint != run_input.input_fingerprint
            or config_fingerprint != run_input.config_fingerprint
        ):
            result[run.scope_id] = Currentness.STALE
        elif run_input.upstream_run_id == upstream_run_id or upstream_run_id in applicable_upstreams.get(
            run.id, set()
        ):
            result[run.scope_id] = Currentness.CURRENT
        else:
            result[run.scope_id] = Currentness.NEEDS_RECONCILIATION
    return result


def classify_extraction_sources(
    session: "Session", *, resolved: "ResolvedExtractionConfig"
) -> dict[UUID, Currentness]:
    """Classify every source with an active extraction run, in a fixed number of statements.

    A source with no active chunking run, or no active extraction run, is
    absent from the returned mapping — not classified, not stale. See
    :class:`Currentness` for what each class means.

    Args:
        session: Active, read-only session.
        resolved: The currently-resolved extraction configuration (see
            :func:`~aizk.graph.extraction.resolve_extraction_config`).

    Returns:
        Each classified source's ``scope_id`` mapped to its :class:`Currentness`.
    """
    return _classify_extraction(session, resolved=resolved)


def classify_extraction_source(
    session: "Session", source_id: UUID, *, resolved: "ResolvedExtractionConfig"
) -> Currentness | None:
    """Classify one source's active extraction run; ``None`` when it is not classified.

    Prefer this to :func:`classify_extraction_sources` when asking about a single
    source: it costs a bounded query set scoped to that source rather than a
    corpus scan. The two agree by construction on any source both classify.

    Args:
        session: Active, read-only session.
        source_id: The source's identity.
        resolved: The currently-resolved extraction configuration.
    """
    return _classify_extraction(session, resolved=resolved, scope_ids=[source_id]).get(source_id)


def stale_extraction_sources(session: "Session", *, resolved: "ResolvedExtractionConfig") -> set[UUID]:
    """Return the scope ids whose active extraction run is :attr:`Currentness.STALE`.

    Staleness never makes a source pending — no staleness condition admits work
    automatically. It marks work an operator may choose to re-admit. A
    :attr:`Currentness.NEEDS_RECONCILIATION` source is re-admittable too but is
    not in this set; :func:`classify_extraction_sources` returns both classes.

    Args:
        session: Active, read-only session.
        resolved: The currently-resolved extraction configuration.

    Returns:
        The stale sources' ``scope_id`` values.
    """
    return {
        source_id
        for source_id, currentness in classify_extraction_sources(session, resolved=resolved).items()
        if currentness is Currentness.STALE
    }


def record_extraction_run_applicability(
    session: "Session",
    *,
    run_id: int,
    upstream_run_id: int,
    input_policy: InputPolicy,
    resolved: "ResolvedExtractionConfig",
) -> ExtractionRunApplicability:
    """Record (or return the existing) applicability of an extraction run to another upstream run.

    Records that the source's chunking or contextualization generation moved on
    to ``upstream_run_id`` but produced output equal to what the active
    extraction run already consumed. The relation's ``input_fingerprint`` is read
    from the upstream run's persisted output fingerprint rather than taken as an
    argument, so a caller cannot record one that disagrees.

    Append-only, with no update path; an identical relation returns the existing
    row. Does **not** commit; the caller owns the transaction.

    Raises:
        ValueError: If ``run_id`` is not an active extraction run; if
            ``upstream_run_id`` is not a chunking or contextualization run
            sharing its ``scope_id``; if the candidate key recomputed from the
            upstream output fingerprint and ``resolved`` does not equal the run's
            ``derivation_key``; if the run has no
            :class:`~aizk.graph.datamodel.ExtractionRunInput`; or if the same
            primary key is already recorded with a different ``input_policy`` or
            ``input_fingerprint``.
    """
    run = session.get(PipelineRun, run_id)
    if run is None or run.status != RunStatus.ACTIVE or run.stage != MENTION_EXTRACTION_STAGE:
        raise ValueError(
            f"run {run_id!r} is not an active {MENTION_EXTRACTION_STAGE!r} run; "
            "applicability may only be recorded against an active extraction run"
        )

    upstream = session.get(PipelineRun, upstream_run_id)
    if upstream is None or upstream.stage not in (CHUNKING_STAGE, VARIANT_STAGE) or upstream.scope_id != run.scope_id:
        raise ValueError(
            f"upstream run {upstream_run_id!r} does not resolve to a chunking or contextualization run "
            f"sharing scope_id={run.scope_id!r}"
        )

    upstream_output_fingerprint = _upstream_output_fingerprint(session, upstream)
    candidate_key = extraction_derivation_key(
        input_fingerprint=upstream_output_fingerprint, config_fingerprint=resolved.config_fingerprint()
    )
    if candidate_key != run.derivation_key:
        raise ValueError(
            f"the candidate key computed from upstream run {upstream_run_id!r}'s output and the "
            f"currently-resolved configuration does not match run {run_id!r}'s derivation_key"
        )

    run_input = session.get(ExtractionRunInput, run_id)
    if run_input is None:
        raise ValueError(f"run {run_id!r} has no recorded ExtractionRunInput; its persist is not complete")

    existing = session.get(ExtractionRunApplicability, (run_id, upstream_run_id))
    if existing is not None:
        if existing.input_policy != input_policy or existing.input_fingerprint != upstream_output_fingerprint:
            raise ValueError(
                f"applicability ({run_id!r}, {upstream_run_id!r}) is already recorded with a different "
                "input_policy or input_fingerprint; there is no update path"
            )
        return existing

    applicability = ExtractionRunApplicability(
        run_id=run_id,
        upstream_run_id=upstream_run_id,
        input_policy=input_policy,
        input_fingerprint=upstream_output_fingerprint,
    )
    session.add(applicability)
    return applicability


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


def _reconcile_active_run(
    engine: "Engine",
    *,
    source_id: UUID,
    input_policy: InputPolicy,
    resolved: "ResolvedExtractionConfig",
    planned_run_id: int,
    planned_upstream_run_id: int,
    planned_input_fingerprint: str,
    chunk_count: int,
) -> DocumentExtractionResult:
    """Reconcile a candidate key that already matches the active extraction run — no extractor call.

    Re-validates the plan inside the write transaction: the active run and the
    resolved upstream run must still be what the read phase planned against, and
    the upstream run's output fingerprint must still equal the planned payload.
    Either mismatch raises :class:`~aizk.graph.contextualization.StalePlanError`
    and commits nothing rather than misrecording provenance.

    When the resolved upstream run differs from what the active run's
    :class:`~aizk.graph.datamodel.ExtractionRunInput` recorded (the
    :attr:`Currentness.NEEDS_RECONCILIATION` case), records
    :class:`~aizk.graph.datamodel.ExtractionRunApplicability` for the new pair.
    The run-input row itself is never rewritten.
    """
    with begin_immediate(engine) as session:
        current_upstream = _replan_upstream(session, source_id=source_id, input_policy=input_policy)
        current_run = active_extraction_run(session, source_id)
        if (
            current_run is None
            or current_run.id != planned_run_id
            or current_upstream.id != planned_upstream_run_id
            or _upstream_output_fingerprint(session, current_upstream) != planned_input_fingerprint
        ):
            raise StalePlanError(
                f"extraction run for source {source_id!r} planned for reuse changed before persist; "
                "retry to re-resolve and re-extract against current state"
            )
        assert current_run.id is not None  # noqa: S101 — an active run is always persisted
        run_input = session.get(ExtractionRunInput, current_run.id)
        assert run_input is not None  # noqa: S101 — its persist is atomic with the run
        if run_input.upstream_run_id != current_upstream.id:
            record_extraction_run_applicability(
                session,
                run_id=current_run.id,
                upstream_run_id=current_upstream.id,
                input_policy=input_policy,
                resolved=resolved,
            )
        mention_count = session.exec(
            select(func.count()).select_from(Mention).where(Mention.run_id == current_run.id)
        ).one()
        result = DocumentExtractionResult(
            run_id=current_run.id,
            source_id=source_id,
            chunk_count=chunk_count,
            mention_count=mention_count,
        )
    logger.info(
        "Reusing extraction run for source: candidate key matches the active run",
        extra={"run_id": result.run_id, "source_id": str(source_id)},
    )
    return result


def extract_document(
    engine: "Engine",
    *,
    source_id: UUID,
    extractor: EntityExtractor,
    input_policy: InputPolicy,
) -> DocumentExtractionResult:
    """Extract and persist one source's mentions, committing the whole document at once.

    Costs at most one ``extractor`` call per chunk, and none when the source's
    active extraction run already covers the payload and configuration the call
    resolves to. That case reconciles instead: no new run and no mentions
    rewritten, only a
    :class:`~aizk.graph.datamodel.ExtractionRunApplicability` row when the
    resolved upstream generation moved on. Otherwise a new run supersedes the
    prior active one and persists its
    :class:`~aizk.graph.datamodel.ExtractionRunInput`, mentions, and
    co-occurrences.

    Opens its own transactions off ``engine`` and commits. Every write for the
    document lands in one of them, so a mid-persist failure leaves no new run and
    no readable mentions from the attempt. The extractor is never invoked while
    that transaction is open, so model latency never holds the writer's lock.

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
            refused before any read or extractor call; or if the source has no
            active chunking run, since an unchunked source has nothing to
            extract.
        StalePlanError: If the active extraction run or the resolved upstream's
            output changed between resolving the work and committing it. Nothing
            is committed. Retryable.
    """
    resolved = ResolvedExtractionConfig(
        extractor_version=extractor.extractor_version,
        materializer_version=MATERIALIZER_VERSION,
        input_policy=input_policy,
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
                chunk_key=chunk_key(chunk),
            )
            for chunk, _, _ in document_order_chunks(read_session, chunking_run.id)
        ]
        upstream_run = _resolve_upstream_run(
            read_session, source_id=source_id, input_policy=input_policy, chunking_run=chunking_run
        )
        assert upstream_run.id is not None  # noqa: S101 — an active run is always persisted
        active_run = active_extraction_run(read_session, source_id)

    # The payload extraction reads, in document order — equal by construction to
    # the consumed upstream run's own persisted output fingerprint.
    planned_input_fingerprint = payload_fingerprint(
        [
            PayloadItem(
                item.chunk_key, item.extraction_input.input_kind, content_fingerprint(item.extraction_input.text)
            )
            for item in work_items
        ]
    )
    candidate_key = extraction_derivation_key(
        input_fingerprint=planned_input_fingerprint, config_fingerprint=resolved.config_fingerprint()
    )

    if active_run is not None and active_run.derivation_key == candidate_key:
        assert active_run.id is not None  # noqa: S101 — an active run is always persisted
        return _reconcile_active_run(
            engine,
            source_id=source_id,
            input_policy=input_policy,
            resolved=resolved,
            planned_run_id=active_run.id,
            planned_upstream_run_id=upstream_run.id,
            planned_input_fingerprint=planned_input_fingerprint,
            chunk_count=len(work_items),
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
        current_upstream = _replan_upstream(session, source_id=source_id, input_policy=input_policy)
        if (
            current_upstream.id != upstream_run.id
            or _upstream_output_fingerprint(session, current_upstream) != planned_input_fingerprint
        ):
            raise StalePlanError(
                f"upstream state for source {source_id!r} changed before persist; "
                "retry to re-resolve and re-extract against current state"
            )
        run = open_extraction_run(
            session, source_id=source_id, resolved=resolved, input_fingerprint=planned_input_fingerprint
        )
        assert run.id is not None  # noqa: S101 — a persisted run always carries an id
        # One immutable ExtractionRunInput row per run: a retry that reuses the
        # active run must not insert a second row under the same run_id.
        if session.get(ExtractionRunInput, run.id) is None:
            session.add(
                ExtractionRunInput(
                    run_id=run.id,
                    upstream_run_id=current_upstream.id,
                    input_policy=input_policy,
                    input_fingerprint=planned_input_fingerprint,
                    config_fingerprint=resolved.config_fingerprint(),
                )
            )
        persisted = persist_mentions(session, run=run, mentions=drafts)
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
    source_id: UUID,
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
    source_ids: "Sequence[UUID]",
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
