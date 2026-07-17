"""Behavioral tests for the corpus dataset's cold-start statistics (``aizk.graph.dataset_stats``).

Seeds a small mention dataset through the real write path
(:func:`~aizk.graph.mention_store.open_extraction_run` /
:func:`~aizk.graph.mention_store.persist_mentions`) across two sources, then
asserts every reported statistic against hand-computed values: mention counts
and the singleton-rate definition (fraction of distinct surface forms
occurring exactly once in the partition, corpus-wide), mentions-per-chunk
(mean over chunks carrying at least one mention of the partition's class),
and co-occurrence density (links per chunk with >=2 mentions, classified by
endpoint anchor-class). Also covers the per-partition, non-additive nature of
singleton counts (a surface with one mention in each class is a singleton in
both partitions but not in the total), the empty-corpus zero-division-safe
case, and that a superseded run's rows are excluded from the corpus dataset.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlmodel import Session, SQLModel, create_engine

from aizk.graph.datamodel import Chunk, ContextualizedChunk, Mention, MentionCooccurrence
from aizk.graph.dataset_stats import compute_dataset_statistics
from aizk.graph.mention_store import MentionDraft, open_extraction_run, persist_mentions
from aizk.pipeline.run import PipelineRun

_SCHEMA_TABLES = [
    Chunk.__table__,
    ContextualizedChunk.__table__,
    Mention.__table__,
    MentionCooccurrence.__table__,
    PipelineRun.__table__,
]

_SOURCE_A = "11111111-1111-1111-1111-111111111111"
_SOURCE_B = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    """A SQLite engine carrying only the chunk/mention/run tables this suite touches."""
    eng = create_engine(f"sqlite:///{tmp_path / 'dataset_stats.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(eng, tables=_SCHEMA_TABLES)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Session:
    """An open session on the schema engine; the test owns commit boundaries."""
    with Session(engine) as sess:
        yield sess


def _seed_chunk(session: Session, *, chunk_id: str, text: str, source_id: str) -> None:
    """Insert and commit a minimal :class:`Chunk` row."""
    session.add(
        Chunk(
            chunk_id=chunk_id,
            content_hash=f"hash-{chunk_id}",
            source_id=source_id,
            heading_path_json="[]",
            ordinal=0,
            text=text,
            char_count=len(text),
        )
    )
    session.commit()


def _seed_contextualized_chunk(
    session: Session, *, chunk_id: str, contextualized_text: str, run_id: int, context_version: int = 1
) -> None:
    """Insert and commit a :class:`ContextualizedChunk` row, the locator target for revision-anchored drafts."""
    session.add(
        ContextualizedChunk(
            run_id=run_id,
            summary_run_id=1,
            chunking_run_id=1,
            chunk_id=chunk_id,
            context_version=context_version,
            contextualized_text=contextualized_text,
            derivation_key="test-derivation-key",
        )
    )
    session.commit()


def _open_run(session: Session, *, source_id: str, upstream_derivation_key: str) -> PipelineRun:
    """Open (or reuse) an extraction run for ``source_id`` and commit it."""
    run = open_extraction_run(
        session,
        source_id=source_id,
        extractor_version="stub/v1",
        materializer_version="1",
        input_policy="raw",
        upstream_derivation_key=upstream_derivation_key,
    )
    session.commit()
    session.refresh(run)
    return run


def _source_draft(*, chunk_id: str, surface_form: str, span: tuple[int, int]) -> MentionDraft:
    """Build a source-anchored, raw-input draft for ``surface_form`` at ``span`` in ``chunk_id``."""
    return MentionDraft(
        chunk_id=chunk_id,
        anchor_kind="source",
        surface_form=surface_form,
        input_kind="raw",
        input_span_start=span[0],
        input_span_end=span[1],
        source_span_start=span[0],
        source_span_end=span[1],
        source_anchor_text=surface_form,
    )


def _revision_draft(
    *,
    chunk_id: str,
    surface_form: str,
    contextualization_run_id: int,
    input_span: tuple[int, int],
    context_version: int = 1,
) -> MentionDraft:
    """Build a revision-anchored, contextualized-input draft for ``surface_form`` in ``chunk_id``."""
    return MentionDraft(
        chunk_id=chunk_id,
        anchor_kind="revision",
        surface_form=surface_form,
        input_kind="contextualized",
        context_version=context_version,
        contextualization_run_id=contextualization_run_id,
        input_span_start=input_span[0],
        input_span_end=input_span[1],
    )


def _seed_corpus(session: Session) -> None:
    """Seed a two-source corpus dataset with a hand-computable statistics profile.

    Source A:

    - ``chunk-a1`` = "Acme Corp met Globex Inc today." — source-anchored
      "Acme Corp" and "Globex Inc", plus a revision-anchored "Investor" read
      from an active contextualized variant. All three mentions share the
      chunk, so co-occurrence linking yields one source-source pair
      (Acme Corp / Globex Inc) and two mixed pairs (each with "Investor").
    - ``chunk-a2`` = "Acme Corp returned." — a second source-anchored "Acme
      Corp" occurrence (so that surface form is *not* a corpus-wide
      singleton), alone in its chunk (no co-occurrence link).
    - ``chunk-a3`` — a raw chunk with two revision-anchored mentions
      ("Founder", "Advisor") from its own active variant, neither present in
      the raw text. Their pair is the sole revision-revision co-occurrence
      link.

    Source B:

    - ``chunk-b1`` = "Initech Corp is here." — a single source-anchored,
      corpus-wide-singleton mention, alone in its chunk (no co-occurrence
      link; excluded from the co-occurrence density denominator).
    """
    _seed_chunk(session, chunk_id="chunk-a1", text="Acme Corp met Globex Inc today.", source_id=_SOURCE_A)
    _seed_chunk(session, chunk_id="chunk-a2", text="Acme Corp returned.", source_id=_SOURCE_A)
    _seed_chunk(session, chunk_id="chunk-a3", text="He founded the firm alone.", source_id=_SOURCE_A)
    _seed_chunk(session, chunk_id="chunk-b1", text="Initech Corp is here.", source_id=_SOURCE_B)

    run_a = _open_run(session, source_id=_SOURCE_A, upstream_derivation_key="a-key-1")
    assert run_a.id is not None
    _seed_contextualized_chunk(
        session,
        chunk_id="chunk-a1",
        contextualized_text="The Investor met with executives.",
        run_id=run_a.id,
    )
    _seed_contextualized_chunk(
        session,
        chunk_id="chunk-a3",
        contextualized_text="The Founder met the Advisor.",
        run_id=run_a.id,
        context_version=2,
    )

    drafts_a = [
        _source_draft(chunk_id="chunk-a1", surface_form="Acme Corp", span=(0, 9)),
        _source_draft(chunk_id="chunk-a1", surface_form="Globex Inc", span=(14, 24)),
        _revision_draft(
            chunk_id="chunk-a1",
            surface_form="Investor",
            contextualization_run_id=run_a.id,
            input_span=(4, 12),
        ),
        _source_draft(chunk_id="chunk-a2", surface_form="Acme Corp", span=(0, 9)),
        _revision_draft(
            chunk_id="chunk-a3",
            surface_form="Founder",
            contextualization_run_id=run_a.id,
            input_span=(4, 11),
            context_version=2,
        ),
        _revision_draft(
            chunk_id="chunk-a3",
            surface_form="Advisor",
            contextualization_run_id=run_a.id,
            input_span=(20, 27),
            context_version=2,
        ),
    ]
    persist_mentions(session, run=run_a, mentions=drafts_a)
    session.commit()

    run_b = _open_run(session, source_id=_SOURCE_B, upstream_derivation_key="b-key-1")
    drafts_b = [_source_draft(chunk_id="chunk-b1", surface_form="Initech Corp", span=(0, 12))]
    persist_mentions(session, run=run_b, mentions=drafts_b)
    session.commit()


def test_dataset_statistics_over_seeded_corpus(session: Session) -> None:
    """Every reported statistic matches its hand-computed value over the seeded two-source corpus."""
    _seed_corpus(session)

    stats = compute_dataset_statistics(session)

    assert stats.source_count == 2

    # Source-anchored partition: "Acme Corp" x2 (not a singleton), "Globex Inc"
    # x1, "Initech Corp" x1 -> 4 mentions, 3 distinct surfaces, 2 singletons.
    assert stats.source.mention_counts.mention_count == 4
    assert stats.source.mention_counts.distinct_surface_form_count == 3
    assert stats.source.mention_counts.singleton_surface_form_count == 2
    assert stats.source.mention_counts.singleton_rate == pytest.approx(2 / 3)
    assert stats.source.mentions_per_chunk.chunk_count == 3  # chunk-a1, chunk-a2, chunk-b1
    assert stats.source.mentions_per_chunk.mentions_per_chunk == pytest.approx(4 / 3)

    # Revision-anchored partition: "Investor", "Founder", "Advisor", each x1 ->
    # 3 mentions, 3 distinct surfaces, all singletons.
    assert stats.revision.mention_counts.mention_count == 3
    assert stats.revision.mention_counts.distinct_surface_form_count == 3
    assert stats.revision.mention_counts.singleton_surface_form_count == 3
    assert stats.revision.mention_counts.singleton_rate == pytest.approx(1.0)
    assert stats.revision.mentions_per_chunk.chunk_count == 2  # chunk-a1, chunk-a3
    assert stats.revision.mentions_per_chunk.mentions_per_chunk == pytest.approx(1.5)

    # Combined total: 7 mentions, 6 distinct surfaces (no cross-class name
    # collisions), 5 singletons (every surface but "Acme Corp").
    assert stats.total.mention_counts.mention_count == 7
    assert stats.total.mention_counts.distinct_surface_form_count == 6
    assert stats.total.mention_counts.singleton_surface_form_count == 5
    assert stats.total.mention_counts.singleton_rate == pytest.approx(5 / 6)
    assert stats.total.mentions_per_chunk.chunk_count == 4  # chunk-a1, chunk-a2, chunk-a3, chunk-b1
    assert stats.total.mentions_per_chunk.mentions_per_chunk == pytest.approx(7 / 4)

    # Co-occurrence density: eligible chunks (>=2 mentions) are chunk-a1 (3
    # mentions) and chunk-a3 (2 mentions); chunk-a2 and chunk-b1 (1 mention
    # each) are excluded.
    assert stats.cooccurrence.eligible_chunk_count == 2
    assert stats.cooccurrence.source_source.link_count == 1  # Acme Corp / Globex Inc, in chunk-a1
    assert stats.cooccurrence.source_source.density == pytest.approx(0.5)
    assert stats.cooccurrence.revision_revision.link_count == 1  # Founder / Advisor, in chunk-a3
    assert stats.cooccurrence.revision_revision.density == pytest.approx(0.5)
    assert stats.cooccurrence.mixed.link_count == 2  # Acme Corp/Investor, Globex Inc/Investor, in chunk-a1
    assert stats.cooccurrence.mixed.density == pytest.approx(1.0)
    assert stats.cooccurrence.total.link_count == 4
    assert stats.cooccurrence.total.density == pytest.approx(2.0)


def test_dataset_statistics_json_round_trips(session: Session) -> None:
    """The returned statistics are a frozen, JSON-serializable snapshot."""
    _seed_corpus(session)

    stats = compute_dataset_statistics(session)

    with pytest.raises(Exception):  # noqa: B017,PT011 - a frozen pydantic model rejects attribute mutation
        stats.source_count = 99  # type: ignore[misc]

    payload = stats.model_dump_json()
    assert '"source_count":2' in payload


def test_dataset_statistics_on_empty_corpus_is_zero_safe(session: Session) -> None:
    """An empty corpus (no active extraction runs) reports all-zero statistics, with no division by zero."""
    stats = compute_dataset_statistics(session)

    assert stats.source_count == 0
    assert stats.source.mention_counts.mention_count == 0
    assert stats.source.mention_counts.singleton_rate == 0.0
    assert stats.source.mentions_per_chunk.mentions_per_chunk == 0.0
    assert stats.revision.mention_counts.mention_count == 0
    assert stats.total.mention_counts.mention_count == 0
    assert stats.cooccurrence.eligible_chunk_count == 0
    assert stats.cooccurrence.total.link_count == 0
    assert stats.cooccurrence.total.density == 0.0


def test_singleton_counts_are_per_partition_and_non_additive(session: Session) -> None:
    """A surface with one source- and one revision-anchored mention is a singleton per class, not in total.

    Singleton counting runs over each partition's own mentions independently:
    the same surface form appearing exactly once in each class partition is a
    singleton in both, yet occurs twice in the combined total — so per-class
    singleton counts do not sum to the total's, unlike mention counts.
    """
    _seed_chunk(session, chunk_id="chunk-1", text="Acme Corp announced results.", source_id=_SOURCE_A)
    run = _open_run(session, source_id=_SOURCE_A, upstream_derivation_key="key-1")
    assert run.id is not None
    _seed_contextualized_chunk(
        session,
        chunk_id="chunk-1",
        contextualized_text="Acme Corp announced its results.",
        run_id=run.id,
    )
    persist_mentions(
        session,
        run=run,
        mentions=[
            _source_draft(chunk_id="chunk-1", surface_form="Acme Corp", span=(0, 9)),
            _revision_draft(
                chunk_id="chunk-1",
                surface_form="Acme Corp",
                contextualization_run_id=run.id,
                input_span=(0, 9),
            ),
        ],
    )
    session.commit()

    stats = compute_dataset_statistics(session)

    # Each class partition sees the surface exactly once: a singleton in both.
    assert stats.source.mention_counts.singleton_surface_form_count == 1
    assert stats.source.mention_counts.singleton_rate == pytest.approx(1.0)
    assert stats.revision.mention_counts.singleton_surface_form_count == 1
    assert stats.revision.mention_counts.singleton_rate == pytest.approx(1.0)

    # Mention counts are additive across partitions; singleton counts are not:
    # in the combined total the surface occurs twice, so it is no singleton.
    assert stats.total.mention_counts.mention_count == 2
    assert stats.total.mention_counts.distinct_surface_form_count == 1
    assert stats.total.mention_counts.singleton_surface_form_count == 0
    assert stats.total.mention_counts.singleton_rate == pytest.approx(0.0)


def test_dataset_statistics_excludes_superseded_runs(session: Session) -> None:
    """Only the union of *active* extraction runs is counted; a superseded run's mentions are excluded."""
    _seed_chunk(session, chunk_id="chunk-1", text="Acme Corp announced results.", source_id=_SOURCE_A)
    run1 = _open_run(session, source_id=_SOURCE_A, upstream_derivation_key="key-1")
    persist_mentions(
        session, run=run1, mentions=[_source_draft(chunk_id="chunk-1", surface_form="Acme Corp", span=(0, 9))]
    )
    session.commit()

    # Re-extract the source with a changed derivation key: opens a superseding run.
    run2 = _open_run(session, source_id=_SOURCE_A, upstream_derivation_key="key-2")
    persist_mentions(
        session, run=run2, mentions=[_source_draft(chunk_id="chunk-1", surface_form="Acme Corp", span=(0, 9))]
    )
    session.commit()

    stats = compute_dataset_statistics(session)

    assert stats.source_count == 1
    assert stats.total.mention_counts.mention_count == 1, "only the active run's mention is counted, not both runs'"
