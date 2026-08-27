"""Behavioral tests for chunk persistence and source-scoped run supersession.

Exercises the ``chunking`` delta's persistence contract through
:mod:`aizk.graph.persistence` on the surrogate-identity model: emitted chunks
round-trip field-for-field (plus their persistence-assigned surrogate ``chunk_id``)
when reconstructed from ``chunk ⋈ chunk_run_manifest ⋈ chunk_run_input ⋈ chunk_run``,
the full emitted set is present in the generation's manifest, identities carry only
stable facts and are reused (by sameness-key) without mutation, and re-chunking a
*source* (scoped by its ``source_id``) into a new generation supersedes the prior
at the run level — recording each generation's own consumed input and span —
without ever touching a prior chunk identity or manifest entry.

The surrogate-identity reuse scenarios (same/different address × content) live in
``test_chunk_identity.py``; this module covers fidelity, generation supersession,
run idempotency, and the persistence-boundary guards.
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, select
import xxhash

from aizk.chunking import SPLITTER_VERSION, Chunk as SplitterChunk, split
from aizk.graph.datamodel import Chunk, ChunkRunManifest
from aizk.graph.persistence import (
    active_chunking_run,
    chunks_of_run,
    current_chunk_ids,
    members_of_run,
    persist_chunks,
    run_input,
)
from aizk.pipeline.run import PipelineRun, RunStatus

_AIZK_UUID = "11111111-1111-1111-1111-111111111111"
_OUTPUT = "output-1"
_OUTPUT_B = "output-2"
_MARKDOWN_HASH = "0011223344556677"
_MARKDOWN_HASH_B = "aabbccddeeff0011"

_SAMPLE_MARKDOWN = """# Title

A paragraph under the title with enough text to be a real chunk.

## Section One

Content for the first section that the splitter carves into a chunk.

## Section Two

Content for the second section, distinct from the first.
"""


def _make_chunk(
    *,
    source_id: str = _AIZK_UUID,
    conversion_output_id: str = _OUTPUT,
    markdown_hash: str = _MARKDOWN_HASH,
    heading_path: tuple[str, ...] = (),
    ordinal: int = 0,
    text: str = "hello world",
    splitter_version: int = SPLITTER_VERSION,
) -> SplitterChunk:
    """Construct a splitter chunk (no chunk_id — identity is assigned at persistence).

    ``source_id`` is the source's ``source_id`` and ``converted_artifact_id`` is the
    per-conversion output locator.
    """
    content_hash = xxhash.xxh64(text.encode("utf-8")).hexdigest()
    return SplitterChunk(
        content_hash=content_hash,
        source_id=source_id,
        heading_path=heading_path,
        ordinal=ordinal,
        text=text,
        char_count=len(text),
        converted_artifact_id=conversion_output_id,
        markdown_hash_xx64=markdown_hash,
        span=(0, len(text)),
        splitter_version=splitter_version,
    )


def test_round_trip_fidelity(session: Session) -> None:
    """Each chunk reconstructed from identity ⋈ manifest ⋈ input ⋈ run equals the persisted chunk."""
    emitted = split(
        _SAMPLE_MARKDOWN,
        source_id=_AIZK_UUID,
        converted_artifact_id=_OUTPUT,
        markdown_hash_xx64=_MARKDOWN_HASH,
    )
    assert emitted, "sample markdown should yield chunks"
    # A heading_path-bearing chunk exercises the tuple<->JSON mapping.
    assert any(c.heading_path for c in emitted)

    run, persisted = persist_chunks(
        session,
        source_id=_AIZK_UUID,
        conversion_output_id=_OUTPUT,
        markdown_hash_xx64=_MARKDOWN_HASH,
        splitter_version=SPLITTER_VERSION,
        chunks=emitted,
    )
    session.commit()

    assert run.id is not None
    # Every persisted chunk carries a surrogate id that split() did not assign.
    assert all(c.chunk_id is not None for c in persisted)
    assert all(e.chunk_id is None for e in emitted), "split() emits no identity"

    reconstructed = {c.chunk_id: c for c in chunks_of_run(session, run.id)}
    assert set(reconstructed) == {c.chunk_id for c in persisted}
    for chunk in persisted:
        assert reconstructed[chunk.chunk_id] == chunk, (
            "every field — including span, generation facts, and the assigned chunk_id — round-trips"
        )


def test_full_set_in_generation(session: Session) -> None:
    """All N emitted chunks are in the generation's manifest, none dropped or added."""
    emitted = split(
        _SAMPLE_MARKDOWN,
        source_id=_AIZK_UUID,
        converted_artifact_id=_OUTPUT,
        markdown_hash_xx64=_MARKDOWN_HASH,
    )
    run, persisted = persist_chunks(
        session,
        source_id=_AIZK_UUID,
        conversion_output_id=_OUTPUT,
        markdown_hash_xx64=_MARKDOWN_HASH,
        splitter_version=SPLITTER_VERSION,
        chunks=emitted,
    )
    session.commit()

    assert run.id is not None
    surrogate_ids = sorted(c.chunk_id for c in persisted)
    assert members_of_run(session, run.id) == surrogate_ids
    assert current_chunk_ids(session, _AIZK_UUID) == set(surrogate_ids)


def _persist_two_generations(session: Session) -> tuple[SplitterChunk, SplitterChunk, SplitterChunk]:
    """Persist gen A {shared, prior_only} then gen B {shared, new_only}; return the three persisted chunks.

    Generation B models a re-conversion of the *same source* (same ``source_id``)
    that produces a new conversion output and new markdown whose content leaves one
    section byte-identical (the shared chunk) while dropping one chunk and adding
    another. The splitter re-emits the unchanged section from gen B's markdown, so
    the shared chunk — reused by its sameness-key ``(source_id, heading_path,
    ordinal, content_hash)`` — keeps gen A's surrogate ``chunk_id``. The returned
    ``shared`` is gen A's persisted chunk (the version recorded in gen A's
    manifest/input).
    """
    shared = _make_chunk(heading_path=("Shared",), ordinal=0, text="unchanged section text")
    shared_rechunked = _make_chunk(
        heading_path=("Shared",),
        ordinal=0,
        text="unchanged section text",
        conversion_output_id=_OUTPUT_B,
        markdown_hash=_MARKDOWN_HASH_B,
    )
    prior_only = _make_chunk(heading_path=("Prior",), ordinal=0, text="only in the prior generation")
    new_only = _make_chunk(
        heading_path=("New",),
        ordinal=0,
        text="only in the new generation",
        conversion_output_id=_OUTPUT_B,
        markdown_hash=_MARKDOWN_HASH_B,
    )

    _run_a, persisted_a = persist_chunks(
        session,
        source_id=_AIZK_UUID,
        conversion_output_id=_OUTPUT,
        markdown_hash_xx64=_MARKDOWN_HASH,
        splitter_version=SPLITTER_VERSION,
        chunks=[shared, prior_only],
    )
    session.commit()
    shared_p, prior_p = persisted_a

    _run_b, persisted_b = persist_chunks(
        session,
        source_id=_AIZK_UUID,
        conversion_output_id=_OUTPUT_B,
        markdown_hash_xx64=_MARKDOWN_HASH_B,
        splitter_version=SPLITTER_VERSION,
        chunks=[shared_rechunked, new_only],
    )
    session.commit()
    shared_b_p, new_p = persisted_b

    assert shared_b_p.chunk_id == shared_p.chunk_id, "byte-identical content reuses the surrogate identity"
    return shared_p, prior_p, new_p


def _superseded_run(session: Session) -> PipelineRun:
    """Return the single superseded chunking run for the source under test."""
    return session.exec(
        select(PipelineRun).where(
            PipelineRun.scope_id == _AIZK_UUID,
            PipelineRun.status == RunStatus.SUPERSEDED,
        )
    ).one()


def test_shared_chunk_current_via_new_generation(session: Session) -> None:
    """A chunk shared across generations stays current by new-gen manifest entry, identity unchanged."""
    shared, _prior_only, _new_only = _persist_two_generations(session)

    active = active_chunking_run(session, _AIZK_UUID)
    assert active is not None and active.id is not None
    assert shared.chunk_id in current_chunk_ids(session, _AIZK_UUID)

    # The identity row carries only stable facts and is unchanged; the generation
    # facts (span, markdown hash, splitter version) are supplied by gen B's run.
    row = session.get(Chunk, shared.chunk_id)
    assert row is not None
    assert (row.text, row.content_hash, str(row.source_id)) == (shared.text, shared.content_hash, shared.source_id)
    reconstructed = {c.chunk_id: c for c in chunks_of_run(session, active.id)}
    # Reconstructed under gen B, the shared chunk carries gen B's conversion output + markdown hash.
    assert reconstructed[shared.chunk_id].converted_artifact_id == _OUTPUT_B
    assert reconstructed[shared.chunk_id].markdown_hash_xx64 == _MARKDOWN_HASH_B


def test_prior_only_chunk_not_current(session: Session) -> None:
    """A chunk only in the prior generation is no longer current but its identity remains intact."""
    _shared, prior_only, _new_only = _persist_two_generations(session)

    assert prior_only.chunk_id not in current_chunk_ids(session, _AIZK_UUID)
    row = session.get(Chunk, prior_only.chunk_id)
    assert row is not None, "the superseded chunk's identity is retained"
    # Recoverable, unmodified, from the generation that emitted it.
    superseded = _superseded_run(session)
    assert superseded.id is not None
    reconstructed = {c.chunk_id: c for c in chunks_of_run(session, superseded.id)}
    assert reconstructed[prior_only.chunk_id] == prior_only


def test_new_only_chunk_current(session: Session) -> None:
    """A chunk only in the new generation is current; no prior identity or manifest is removed."""
    shared, prior_only, new_only = _persist_two_generations(session)

    assert new_only.chunk_id in current_chunk_ids(session, _AIZK_UUID)

    # The prior generation's manifest is intact and untouched.
    superseded = _superseded_run(session)
    assert superseded.id is not None
    assert sorted(members_of_run(session, superseded.id)) == sorted([shared.chunk_id, prior_only.chunk_id])
    # Every manifest entry ever written still exists (append-only).
    all_entries = session.exec(select(ChunkRunManifest)).all()
    assert len(all_entries) == 4  # gen A: {shared, prior_only}; gen B: {shared, new_only}


def test_superseded_generation_consumed_input_and_manifest_recoverable(session: Session) -> None:
    """A superseded generation's recorded input and manifest remain present and unmodified."""
    shared, prior_only, _new_only = _persist_two_generations(session)

    superseded = _superseded_run(session)
    assert superseded.id is not None

    # What gen A consumed is recoverable: its locator and verification hash.
    consumed = run_input(session, superseded.id)
    assert consumed is not None
    assert consumed.conversion_output_id == _OUTPUT
    assert consumed.markdown_hash_xx64 == _MARKDOWN_HASH

    # What gen A produced is recoverable without re-running the splitter.
    assert sorted(members_of_run(session, superseded.id)) == sorted([shared.chunk_id, prior_only.chunk_id])
    reconstructed = {c.chunk_id: c for c in chunks_of_run(session, superseded.id)}
    assert reconstructed[shared.chunk_id] == shared
    assert reconstructed[prior_only.chunk_id] == prior_only


# --------------------------------------------------------------------------- #
# Run-idempotency: unchanged rerun reuses the run; version bump supersedes
# --------------------------------------------------------------------------- #


def test_unchanged_rerun_reuses_run_without_manifest_churn(session: Session) -> None:
    """Re-persisting unchanged chunks reuses the active run: no new run, no extra manifest rows."""
    chunk = _make_chunk(text="stable content")
    first, _ = persist_chunks(
        session,
        source_id=_AIZK_UUID,
        conversion_output_id=_OUTPUT,
        markdown_hash_xx64=_MARKDOWN_HASH,
        splitter_version=SPLITTER_VERSION,
        chunks=[chunk],
    )
    session.commit()
    manifest_after_first = len(session.exec(select(ChunkRunManifest)).all())

    second, _ = persist_chunks(
        session,
        source_id=_AIZK_UUID,
        conversion_output_id=_OUTPUT,
        markdown_hash_xx64=_MARKDOWN_HASH,
        splitter_version=SPLITTER_VERSION,
        chunks=[chunk],
    )
    session.commit()

    assert second.id == first.id, "the unchanged rerun reuses the active run"
    runs = session.exec(select(PipelineRun).where(PipelineRun.scope_id == _AIZK_UUID)).all()
    assert len(runs) == 1
    assert runs[0].status == RunStatus.ACTIVE
    assert len(session.exec(select(ChunkRunManifest)).all()) == manifest_after_first


def test_splitter_version_bump_supersedes_even_when_content_unchanged(session: Session) -> None:
    """A splitter_version bump opens a superseding run although the content (sameness-key) is unchanged."""
    chunk_v1 = _make_chunk(text="unchanged content", splitter_version=SPLITTER_VERSION)
    first, persisted_v1 = persist_chunks(
        session,
        source_id=_AIZK_UUID,
        conversion_output_id=_OUTPUT,
        markdown_hash_xx64=_MARKDOWN_HASH,
        splitter_version=SPLITTER_VERSION,
        chunks=[chunk_v1],
    )
    session.commit()

    chunk_v2 = _make_chunk(text="unchanged content", splitter_version=SPLITTER_VERSION + 1)
    second, persisted_v2 = persist_chunks(
        session,
        source_id=_AIZK_UUID,
        conversion_output_id=_OUTPUT,
        markdown_hash_xx64=_MARKDOWN_HASH,
        splitter_version=SPLITTER_VERSION + 1,
        chunks=[chunk_v2],
    )
    session.commit()

    assert second.id != first.id
    # Content is unchanged, so the surrogate identity is reused across the version bump.
    assert persisted_v2[0].chunk_id == persisted_v1[0].chunk_id, "sameness-key reuse ignores splitter_version"
    statuses = {
        r.id: r.status for r in session.exec(select(PipelineRun).where(PipelineRun.scope_id == _AIZK_UUID)).all()
    }
    assert statuses == {first.id: RunStatus.SUPERSEDED, second.id: RunStatus.ACTIVE}


# --------------------------------------------------------------------------- #
# Provenance guard: chunk fields must match the run keyed by them
# --------------------------------------------------------------------------- #


def test_reuse_requires_matching_spans_not_just_ids(session: Session) -> None:
    """The unchanged-run fast path compares the full manifest (chunk_id + span), not ids alone.

    Re-presenting the same sameness-key under the same derivation key but a
    different span must not be silently absorbed into the active run as if
    unchanged; the manifest comparison includes the span.
    """
    chunk = _make_chunk(text="stable content")
    first, persisted_first = persist_chunks(
        session,
        source_id=_AIZK_UUID,
        conversion_output_id=_OUTPUT,
        markdown_hash_xx64=_MARKDOWN_HASH,
        splitter_version=SPLITTER_VERSION,
        chunks=[chunk],
    )
    session.commit()

    # Same sameness-key (span is not part of it), but a shifted span.
    shifted = chunk.model_copy(update={"span": (5, 5 + len(chunk.text))})
    second, persisted_second = persist_chunks(
        session,
        source_id=_AIZK_UUID,
        conversion_output_id=_OUTPUT,
        markdown_hash_xx64=_MARKDOWN_HASH,
        splitter_version=SPLITTER_VERSION,
        chunks=[shifted],
    )
    session.commit()

    assert second.id != first.id, "a span change is not treated as an unchanged rerun"
    # The chunk is content-identical, so its surrogate identity is reused (the row already exists).
    assert persisted_second[0].chunk_id == persisted_first[0].chunk_id
    statuses = {
        r.id: r.status for r in session.exec(select(PipelineRun).where(PipelineRun.scope_id == _AIZK_UUID)).all()
    }
    assert statuses == {first.id: RunStatus.SUPERSEDED, second.id: RunStatus.ACTIVE}


def test_persist_rejects_sameness_key_collision_with_conflicting_facts(session: Session) -> None:
    """An incoming chunk whose sameness-key matches an existing row but whose text differs is rejected.

    Guards the surrogate-reuse invariant against a ``content_hash`` collision (or a
    caller that forges a colliding ``content_hash``); reusing the row would repoint
    the manifest at the wrong source text.
    """
    original = _make_chunk(text="original content")
    persist_chunks(
        session,
        source_id=_AIZK_UUID,
        conversion_output_id=_OUTPUT,
        markdown_hash_xx64=_MARKDOWN_HASH,
        splitter_version=SPLITTER_VERSION,
        chunks=[original],
    )
    session.commit()

    # A forged chunk sharing original's sameness-key (same source_id/heading_path/ordinal
    # and a colliding content_hash) but carrying different text; a new generation (new
    # markdown hash) forces the write path, not the reuse fast path.
    colliding = SplitterChunk(
        content_hash=original.content_hash,
        source_id=_AIZK_UUID,
        heading_path=(),
        ordinal=0,
        text="different content",
        char_count=len("different content"),
        converted_artifact_id=_OUTPUT_B,
        markdown_hash_xx64=_MARKDOWN_HASH_B,
        span=(0, len("different content")),
        splitter_version=SPLITTER_VERSION,
    )

    with pytest.raises(ValueError, match="content_hash collision"):
        persist_chunks(
            session,
            source_id=_AIZK_UUID,
            conversion_output_id=_OUTPUT_B,
            markdown_hash_xx64=_MARKDOWN_HASH_B,
            splitter_version=SPLITTER_VERSION,
            chunks=[colliding],
        )
    # The existing identity is unmodified and no second generation was opened.
    rows = session.exec(select(Chunk)).all()
    assert len(rows) == 1 and rows[0].text == "original content"
    assert len(session.exec(select(PipelineRun).where(PipelineRun.scope_id == _AIZK_UUID)).all()) == 1


@pytest.mark.parametrize(
    "bad_chunk",
    [
        pytest.param(_make_chunk(markdown_hash="ffffffffffffffff", text="x"), id="markdown_hash"),
        pytest.param(_make_chunk(text="x", splitter_version=SPLITTER_VERSION + 1), id="splitter_version"),
        pytest.param(_make_chunk(conversion_output_id="other-output", text="x"), id="conversion_output_id"),
        pytest.param(_make_chunk(source_id="22222222-2222-2222-2222-222222222222", text="x"), id="source_id"),
    ],
)
def test_persist_rejects_chunk_provenance_mismatch(session: Session, bad_chunk: SplitterChunk) -> None:
    """A chunk whose provenance disagrees with the run's keying values is rejected, recording nothing."""
    with pytest.raises(ValueError, match="do not match the run provenance"):
        persist_chunks(
            session,
            source_id=_AIZK_UUID,
            conversion_output_id=_OUTPUT,
            markdown_hash_xx64=_MARKDOWN_HASH,
            splitter_version=SPLITTER_VERSION,
            chunks=[bad_chunk],
        )
    assert session.exec(select(PipelineRun).where(PipelineRun.scope_id == _AIZK_UUID)).all() == []
