"""Run a foreground extraction pass over selected sources.

Explicit source lists run without confirmation. Corpus scans require confirmation
and use the active chunking runs to select sources.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlmodel import Session, select

from aizk.extraction.processing.document import extract_document
from aizk.graph.chunks import CHUNKING_STAGE
from aizk.pipeline.invalidation import require_reprocessing_confirmation
from aizk.pipeline.run import PipelineRun, RunStatus

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy import Engine

    from aizk.extraction.core.extractors import EntityExtractor
    from aizk.extraction.processing.document import DocumentExtractionResult
    from aizk.graph.mentions import InputPolicy


def extract_corpus(
    engine: "Engine",
    *,
    source_ids: "Sequence[UUID]",
    extractor: EntityExtractor,
    input_policy: InputPolicy,
) -> list[DocumentExtractionResult]:
    """Extract sources in the given order, one document transaction at a time.

    Each source commits independently. A failure propagates after prior sources
    commit, and the function returns no partial result list. Repeating the same
    inputs is safe because each document operation is idempotent.

    Args:
        engine: Shared engine passed to each document operation.
        source_ids: Sources to process in order.
        extractor: Shared injected NER extractor.
        input_policy: Input policy shared by all sources.

    Returns:
        One result per source, in ``source_ids`` order.
    """
    return [
        extract_document(engine, source_id=source_id, extractor=extractor, input_policy=input_policy)
        for source_id in source_ids
    ]


def resolve_target_source_ids(
    session: "Session",
    *,
    source_ids: "Sequence[UUID] | None" = None,
    limit: int | None = None,
) -> list[UUID]:
    """Resolve the target sources for a dataset run.

    Use explicit source IDs in input order after removing duplicates. When
    ``source_ids`` is ``None``, return active chunking sources ordered by
    ``(created_at, scope_id)`` and apply ``limit``.

    Args:
        session: Active, read-only session.
        source_ids: Explicit sources, or ``None`` for a corpus scan.
        limit: Maximum corpus-scan target count.

    Returns:
        The resolved source IDs.
    """
    if source_ids is not None:
        seen: set[UUID] = set()
        deduped: list[UUID] = []
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
    source_ids: "Sequence[UUID] | None",
    limit: int | None,
    confirmed: bool,
    extractor: "EntityExtractor",
    input_policy: "InputPolicy",
) -> list["DocumentExtractionResult"]:
    """Resolve targets, require confirmation for corpus scans, and extract.

    Explicit source IDs bypass confirmation. Corpus scans require ``confirmed=True``.
    Repeating unchanged inputs reuses active extraction runs.

    Args:
        engine: Shared engine passed to extraction.
        source_ids: Explicit sources, or ``None`` for a corpus scan.
        limit: Maximum corpus-scan target count.
        confirmed: Approval for a corpus scan.
        extractor: Injected NER extractor.
        input_policy: Raw or contextualized input policy.

    Returns:
        One result per target source, in resolution order.

    Raises:
        ReprocessingConfirmationError: If a corpus scan is not confirmed.
    """
    with Session(engine) as session:
        targets = resolve_target_source_ids(session, source_ids=source_ids, limit=limit)

    if source_ids is None:
        require_reprocessing_confirmation("corpus-scanning extraction dataset run", confirmed=confirmed)

    return extract_corpus(engine, source_ids=targets, extractor=extractor, input_policy=input_policy)
