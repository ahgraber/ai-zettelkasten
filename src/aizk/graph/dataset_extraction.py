"""Foreground dataset-run orchestration: target-source selection, the confirmation gate, and extraction.

The dataset run is a distinct entry point from the extraction worker
(:mod:`aizk.graph.extraction_worker`): it drives
:func:`~aizk.graph.extraction_run.extract_corpus` synchronously in the calling
process, over an explicitly-selected target-source set, to produce the
mention dataset the canonicalization change is calibrated against — rather
than claiming work-units from :class:`~aizk.graph.datamodel.ExtractionJob`
through the shared runtime.

:func:`resolve_target_source_ids` selects that target set; when the caller
does not name sources explicitly, the set is built by scanning the corpus (all
sources with an active chunking run, or a bounded ``limit`` sample of them),
so :func:`run_dataset_extraction` gates that case behind explicit confirmation
via :func:`aizk.pipeline.invalidation.require_reprocessing_confirmation` — the
same gate and mechanism
:func:`aizk.graph.extraction_workunit.enqueue_extraction_backfill` uses for its
corpus-wide backfill enqueue. An explicit, operator-named source list is never
gated, mirroring how :func:`aizk.graph.extraction_workunit.enqueue_extraction`
enqueues one named source without confirmation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlmodel import Session, select

from aizk.graph.extraction_run import extract_corpus
from aizk.graph.persistence import CHUNKING_STAGE
from aizk.pipeline.invalidation import require_reprocessing_confirmation
from aizk.pipeline.run import PipelineRun, RunStatus

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy import Engine

    from aizk.graph.extraction import EntityExtractor
    from aizk.graph.extraction_run import DocumentExtractionResult
    from aizk.graph.mention_store import InputPolicy


def resolve_target_source_ids(
    session: "Session",
    *,
    source_ids: "Sequence[str] | None" = None,
    limit: int | None = None,
) -> list[str]:
    """Resolve a dataset run's target sources.

    An explicit ``source_ids`` sequence is used verbatim (order-preserved,
    duplicates removed) — an operator-named target set. Otherwise every source
    with an active chunking run is the target set (an unchunked source has
    nothing to extract, mirroring
    :func:`aizk.graph.extraction_run.extract_document`'s own rejection),
    ordered by run-creation time with ``scope_id`` as the tiebreaker — a total
    order, so the sample stays reproducible even when several runs share one
    ``created_at`` timestamp; ``limit`` caps that set to its first N sources
    when supplied.

    Args:
        session: Active, read-only session.
        source_ids: Explicit target sources, or ``None`` to scan the corpus.
        limit: Caps a corpus scan's target set to its first N sources; ignored
            when ``source_ids`` is supplied.

    Returns:
        The resolved target ``source_id`` list.
    """
    if source_ids is not None:
        seen: set[str] = set()
        deduped: list[str] = []
        for source_id in source_ids:
            if source_id not in seen:
                seen.add(source_id)
                deduped.append(source_id)
        return deduped

    corpus_source_ids = list(
        session.exec(
            select(PipelineRun.scope_id)
            .where(PipelineRun.stage == CHUNKING_STAGE, PipelineRun.status == RunStatus.ACTIVE)
            # scope_id breaks created_at ties (a total order), so a --limit
            # sample is reproducible even across same-timestamp runs.
            .order_by(PipelineRun.created_at, PipelineRun.scope_id)
        ).all()
    )
    return corpus_source_ids[:limit] if limit is not None else corpus_source_ids


def run_dataset_extraction(
    engine: "Engine",
    *,
    source_ids: "Sequence[str] | None",
    limit: int | None,
    confirmed: bool,
    extractor: "EntityExtractor",
    input_policy: "InputPolicy",
) -> list["DocumentExtractionResult"]:
    """Resolve target sources, gate a corpus-scanning selection behind confirmation, and extract.

    Only an **implicit** selection — a full corpus scan or a ``limit``-bounded
    sample of it — is confirmation-gated. An explicit ``source_ids``
    enumeration is treated as deliberate operator intent regardless of its
    length and therefore bypasses the gate: the operator named every target,
    so there is no unreviewed blast radius for the gate to guard.

    Re-running with unchanged inputs is a cheap no-op: each target source's
    extraction reuses its active run when the run's derivation key is
    unchanged (see :func:`aizk.graph.mention_store.open_extraction_run`), so
    invoking this repeatedly over an unchanged corpus writes no new rows.

    Args:
        engine: The shared engine; passed through to
            :func:`~aizk.graph.extraction_run.extract_corpus`.
        source_ids: Explicit target sources, or ``None`` to scan the corpus
            (see :func:`resolve_target_source_ids`).
        limit: Caps a corpus scan's target set; ignored when ``source_ids`` is
            supplied.
        confirmed: Explicit human approval for a corpus-scanning selection;
            ignored when ``source_ids`` is supplied (an explicit target set is
            never gated).
        extractor: The injected NER extractor.
        input_policy: The raw-vs-contextualized input toggle.

    Returns:
        One :class:`~aizk.graph.extraction_run.DocumentExtractionResult` per
        target source, in resolution order.

    Raises:
        ReprocessingConfirmationError: If ``source_ids`` is ``None`` (a
            corpus-scanning selection) and ``confirmed`` is ``False``.
    """
    with Session(engine) as session:
        targets = resolve_target_source_ids(session, source_ids=source_ids, limit=limit)

    if source_ids is None:
        require_reprocessing_confirmation("corpus-scanning extraction dataset run", confirmed=confirmed)

    return extract_corpus(engine, source_ids=targets, extractor=extractor, input_policy=input_policy)
