"""Compute read-only cold-start statistics for the active corpus mention dataset.

The dataset is the union of mentions and co-occurrences in each source's active
extraction run. Statistics include source, revision, combined, and co-occurrence
partitions and return as a JSON-serializable :class:`DatasetStatistics`.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import select

from aizk.graph.datamodel import ANCHOR_KIND_SOURCE, Mention, MentionCooccurrence
from aizk.graph.mentions import MENTION_EXTRACTION_STAGE
from aizk.pipeline.run import PipelineRun, RunStatus

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlmodel import Session


class MentionCountStatistics(BaseModel):
    """Mention volume and singleton rate for one dataset partition.

    Attributes:
        mention_count: Total mentions in the partition.
        distinct_surface_form_count: Distinct ``surface_form`` values in the
            partition.
        singleton_surface_form_count: Distinct surface forms occurring exactly
            once in the partition.
        singleton_rate: ``singleton_surface_form_count / distinct_surface_form_count``,
            or ``0.0`` when the partition has no mentions.
    """

    model_config = ConfigDict(frozen=True)

    mention_count: int = Field(ge=0)
    distinct_surface_form_count: int = Field(ge=0)
    singleton_surface_form_count: int = Field(ge=0)
    singleton_rate: float = Field(ge=0.0, le=1.0)


class ChunkDensityStatistics(BaseModel):
    """Mentions-per-chunk density for one dataset partition.

    Attributes:
        chunk_count: Distinct chunks carrying at least one mention of the
            partition's class(es) — the denominator ``mentions_per_chunk`` is
            averaged over.
        mentions_per_chunk: ``mention_count / chunk_count`` for the same
            partition, or ``0.0`` when ``chunk_count`` is zero.
    """

    model_config = ConfigDict(frozen=True)

    chunk_count: int = Field(ge=0)
    mentions_per_chunk: float = Field(ge=0.0)


class AnchorClassStatistics(BaseModel):
    """Cold-start statistics for one mention ``anchor_kind`` partition (or the combined total)."""

    model_config = ConfigDict(frozen=True)

    mention_counts: MentionCountStatistics
    mentions_per_chunk: ChunkDensityStatistics


class CooccurrenceClassStatistics(BaseModel):
    """Co-occurrence link volume and density for one link-endpoint-class partition.

    Attributes:
        link_count: Co-occurrence links in this class.
        density: ``link_count / eligible_chunk_count`` (see
            :attr:`CooccurrenceStatistics.eligible_chunk_count`), or ``0.0``
            when there are no eligible chunks.
    """

    model_config = ConfigDict(frozen=True)

    link_count: int = Field(ge=0)
    density: float = Field(ge=0.0)


class CooccurrenceStatistics(BaseModel):
    """Co-occurrence link counts and density by endpoint class.

    Density uses eligible chunks, defined as chunks with at least two mentions.
    All partitions share that denominator. ``source_source``,
    ``revision_revision``, and ``mixed`` are exclusive and sum to ``total``.

    Attributes:
        eligible_chunk_count: Shared density denominator.
        source_source: Links with two source-anchored endpoints.
        revision_revision: Links with two revision-anchored endpoints.
        mixed: Links with one endpoint of each class.
        total: All co-occurrence links.
    """

    model_config = ConfigDict(frozen=True)

    eligible_chunk_count: int = Field(ge=0)
    source_source: CooccurrenceClassStatistics
    revision_revision: CooccurrenceClassStatistics
    mixed: CooccurrenceClassStatistics
    total: CooccurrenceClassStatistics


class DatasetStatistics(BaseModel):
    """Statistics over active extraction runs in the corpus dataset.

    Mention counts sum across source and revision partitions. Singleton counts do
    not, because each partition counts surface forms independently.

    Attributes:
        source_count: Number of contributing sources.
        source: Source-anchored mention statistics.
        revision: Revision-anchored mention statistics.
        total: Combined mention statistics.
        cooccurrence: Link density by endpoint class.
    """

    model_config = ConfigDict(frozen=True)

    source_count: int = Field(ge=0)
    source: AnchorClassStatistics
    revision: AnchorClassStatistics
    total: AnchorClassStatistics
    cooccurrence: CooccurrenceStatistics


class _MentionRow(BaseModel):
    """Internal projection of the columns statistics computation needs from one mention."""

    model_config = ConfigDict(frozen=True)

    mention_id: UUID
    chunk_id: UUID
    anchor_kind: str
    surface_form: str


def _anchor_class_statistics(rows: "Sequence[_MentionRow]") -> AnchorClassStatistics:
    """Compute :class:`AnchorClassStatistics` over a mention-row subset (one partition)."""
    mention_count = len(rows)
    surface_form_counts = Counter(row.surface_form for row in rows)
    distinct_surface_form_count = len(surface_form_counts)
    singleton_surface_form_count = sum(1 for count in surface_form_counts.values() if count == 1)
    singleton_rate = singleton_surface_form_count / distinct_surface_form_count if distinct_surface_form_count else 0.0

    chunk_mention_counts = Counter(row.chunk_id for row in rows)
    chunk_count = len(chunk_mention_counts)
    mentions_per_chunk = mention_count / chunk_count if chunk_count else 0.0

    return AnchorClassStatistics(
        mention_counts=MentionCountStatistics(
            mention_count=mention_count,
            distinct_surface_form_count=distinct_surface_form_count,
            singleton_surface_form_count=singleton_surface_form_count,
            singleton_rate=singleton_rate,
        ),
        mentions_per_chunk=ChunkDensityStatistics(chunk_count=chunk_count, mentions_per_chunk=mentions_per_chunk),
    )


def _cooccurrence_class(class_lo: str, class_hi: str) -> str:
    """Classify a link by its two endpoints' ``anchor_kind`` values."""
    if class_lo == ANCHOR_KIND_SOURCE and class_hi == ANCHOR_KIND_SOURCE:
        return "source_source"
    if class_lo != ANCHOR_KIND_SOURCE and class_hi != ANCHOR_KIND_SOURCE:
        return "revision_revision"
    return "mixed"


def _cooccurrence_statistics(
    rows: "Sequence[_MentionRow]",
    links: "Sequence[tuple[UUID, UUID, UUID]]",
) -> CooccurrenceStatistics:
    """Compute :class:`CooccurrenceStatistics` from the corpus dataset's mentions and co-occurrence links.

    Args:
        rows: Every mention in the corpus dataset.
        links: Every co-occurrence link as ``(mention_id_lo, mention_id_hi, chunk_id)`` triples.
    """
    chunk_mention_counts = Counter(row.chunk_id for row in rows)
    eligible_chunk_count = sum(1 for count in chunk_mention_counts.values() if count >= 2)

    anchor_by_mention_id = {row.mention_id: row.anchor_kind for row in rows}
    link_class_counts: Counter[str] = Counter()
    for mention_id_lo, mention_id_hi, _chunk_id in links:
        link_class_counts[
            _cooccurrence_class(anchor_by_mention_id[mention_id_lo], anchor_by_mention_id[mention_id_hi])
        ] += 1

    def _class_stats(link_count: int) -> CooccurrenceClassStatistics:
        density = link_count / eligible_chunk_count if eligible_chunk_count else 0.0
        return CooccurrenceClassStatistics(link_count=link_count, density=density)

    source_source = link_class_counts["source_source"]
    revision_revision = link_class_counts["revision_revision"]
    mixed = link_class_counts["mixed"]

    return CooccurrenceStatistics(
        eligible_chunk_count=eligible_chunk_count,
        source_source=_class_stats(source_source),
        revision_revision=_class_stats(revision_revision),
        mixed=_class_stats(mixed),
        total=_class_stats(source_source + revision_revision + mixed),
    )


def compute_dataset_statistics(session: "Session") -> DatasetStatistics:
    """Compute read-only statistics over the active mention dataset.

    Superseded runs are excluded. The result contains mention and co-occurrence
    statistics for source, revision, and combined partitions.

    Args:
        session: Active, read-only session.

    Returns:
        The computed :class:`DatasetStatistics`.
    """
    active_runs = session.exec(
        select(PipelineRun.id, PipelineRun.scope_id).where(
            PipelineRun.stage == MENTION_EXTRACTION_STAGE,
            PipelineRun.status == RunStatus.ACTIVE,
        )
    ).all()
    run_ids = [run_id for run_id, _scope_id in active_runs]
    source_count = len({scope_id for _run_id, scope_id in active_runs})

    rows: list[_MentionRow] = []
    links: list[tuple[UUID, UUID, UUID]] = []
    if run_ids:
        rows = [
            _MentionRow(mention_id=mention_id, chunk_id=chunk_id, anchor_kind=anchor_kind, surface_form=surface_form)
            for mention_id, chunk_id, anchor_kind, surface_form in session.exec(
                select(Mention.mention_id, Mention.chunk_id, Mention.anchor_kind, Mention.surface_form).where(
                    Mention.run_id.in_(run_ids)
                )
            ).all()
        ]
        links = list(
            session.exec(
                select(
                    MentionCooccurrence.mention_id_lo,
                    MentionCooccurrence.mention_id_hi,
                    MentionCooccurrence.chunk_id,
                ).where(MentionCooccurrence.run_id.in_(run_ids))
            ).all()
        )

    source_rows = [row for row in rows if row.anchor_kind == ANCHOR_KIND_SOURCE]
    revision_rows = [row for row in rows if row.anchor_kind != ANCHOR_KIND_SOURCE]

    return DatasetStatistics(
        source_count=source_count,
        source=_anchor_class_statistics(source_rows),
        revision=_anchor_class_statistics(revision_rows),
        total=_anchor_class_statistics(rows),
        cooccurrence=_cooccurrence_statistics(rows, links),
    )
