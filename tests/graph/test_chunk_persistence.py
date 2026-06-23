"""Behavioral tests for chunk persistence and source-scoped run supersession.

Exercises the ``chunking`` delta's persistence contract through
:mod:`aizk.graph.persistence` on the stable-identity model: emitted chunks
round-trip field-for-field when reconstructed from ``chunk ⋈ chunk_run_manifest ⋈
chunk_run_input ⋈ chunk_run``, the full emitted set is present in the
generation's manifest, content-addressed identities carry only stable facts and
are reused without mutation, and re-chunking a *source* (scoped by its
``source_id``) into a new generation supersedes the prior at the run level —
recording each generation's own consumed input and span — without ever touching a
prior chunk identity or manifest entry.
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, select
import xxhash

from aizk.chunking import SPLITTER_VERSION, Chunk as SplitterChunk, split
from aizk.chunking.datamodel import derive_chunk_id
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
    """Construct a splitter chunk with a content-addressed id for controlled tests.

    ``source_id`` is the source's ``source_id`` and ``converted_artifact_id`` is the
    per-conversion output locator, matching the revised model.
    """
    content_hash = xxhash.xxh64(text.encode("utf-8")).hexdigest()
    chunk_id = derive_chunk_id(source_id, heading_path, ordinal, content_hash)
    return SplitterChunk(
        chunk_id=chunk_id,
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
    """Each chunk reconstructed from identity ⋈ manifest ⋈ input ⋈ run equals the emitted chunk."""
    emitted = split(
        _SAMPLE_MARKDOWN,
        source_id=_AIZK_UUID,
        converted_artifact_id=_OUTPUT,
        markdown_hash_xx64=_MARKDOWN_HASH,
    )
    assert emitted, "sample markdown should yield chunks"
    # A heading_path-bearing chunk exercises the tuple<->JSON mapping.
    assert any(c.heading_path for c in emitted)

    run = persist_chunks(
        session,
        source_id=_AIZK_UUID,
        conversion_output_id=_OUTPUT,
        markdown_hash_xx64=_MARKDOWN_HASH,
        splitter_version=SPLITTER_VERSION,
        chunks=emitted,
    )
    session.commit()

    assert run.id is not None
    reconstructed = {c.chunk_id: c for c in chunks_of_run(session, run.id)}
    assert set(reconstructed) == {c.chunk_id for c in emitted}
    for chunk in emitted:
        assert reconstructed[chunk.chunk_id] == chunk, (
            "every field — including span and generation facts — round-trips"
        )


def test_full_set_in_generation(session: Session) -> None:
    """All N emitted chunks are in the generation's manifest, none dropped or added."""
    emitted = split(
        _SAMPLE_MARKDOWN,
        source_id=_AIZK_UUID,
        converted_artifact_id=_OUTPUT,
        markdown_hash_xx64=_MARKDOWN_HASH,
    )
    run = persist_chunks(
        session,
        source_id=_AIZK_UUID,
        conversion_output_id=_OUTPUT,
        markdown_hash_xx64=_MARKDOWN_HASH,
        splitter_version=SPLITTER_VERSION,
        chunks=emitted,
    )
    session.commit()

    assert run.id is not None
    assert members_of_run(session, run.id) == sorted(c.chunk_id for c in emitted)
    assert current_chunk_ids(session, _AIZK_UUID) == {c.chunk_id for c in emitted}


def test_reinsert_reuses_identity(session: Session) -> None:
    """Re-persisting an existing chunk_id creates no duplicate identity and mutates no row."""
    chunk = _make_chunk(text="stable content")
    run = persist_chunks(
        session,
        source_id=_AIZK_UUID,
        conversion_output_id=_OUTPUT,
        markdown_hash_xx64=_MARKDOWN_HASH,
        splitter_version=SPLITTER_VERSION,
        chunks=[chunk],
    )
    session.commit()

    # Re-persist the identical chunk (an accidental re-process of the same inputs).
    persist_chunks(
        session,
        source_id=_AIZK_UUID,
        conversion_output_id=_OUTPUT,
        markdown_hash_xx64=_MARKDOWN_HASH,
        splitter_version=SPLITTER_VERSION,
        chunks=[chunk],
    )
    session.commit()

    rows = session.exec(select(Chunk).where(Chunk.chunk_id == chunk.chunk_id)).all()
    assert len(rows) == 1, "no duplicate identity for a re-persisted chunk_id"
    assert run.id is not None
    assert chunks_of_run(session, run.id) == [chunk]


def test_novel_chunk_stored_once(session: Session) -> None:
    """A chunk_id not present is stored as exactly one new identity carrying its stable facts."""
    chunk = _make_chunk(text="a brand new chunk")
    assert session.get(Chunk, chunk.chunk_id) is None

    run = persist_chunks(
        session,
        source_id=_AIZK_UUID,
        conversion_output_id=_OUTPUT,
        markdown_hash_xx64=_MARKDOWN_HASH,
        splitter_version=SPLITTER_VERSION,
        chunks=[chunk],
    )
    session.commit()

    rows = session.exec(select(Chunk).where(Chunk.chunk_id == chunk.chunk_id)).all()
    assert len(rows) == 1
    assert run.id is not None
    assert chunks_of_run(session, run.id) == [chunk]


def _persist_two_generations(session: Session) -> tuple[SplitterChunk, SplitterChunk, SplitterChunk]:
    """Persist gen A {shared, prior_only} then gen B {shared, new_only}; return the three chunks.

    Generation B models a re-conversion of the *same source* (same ``source_id``)
    that produces a new conversion output and new markdown whose content leaves one
    section byte-identical (the shared chunk) while dropping one chunk and adding
    another. The splitter re-emits the unchanged section from gen B's markdown, so
    the shared chunk — being content-addressed on ``(source_id, heading_path,
    ordinal, content_hash)`` — keeps gen A's ``chunk_id``. The returned ``shared``
    is gen A's emitted chunk (the version recorded in gen A's manifest/input).
    """
    shared = _make_chunk(heading_path=("Shared",), ordinal=0, text="unchanged section text")
    shared_rechunked = _make_chunk(
        heading_path=("Shared",),
        ordinal=0,
        text="unchanged section text",
        conversion_output_id=_OUTPUT_B,
        markdown_hash=_MARKDOWN_HASH_B,
    )
    assert shared_rechunked.chunk_id == shared.chunk_id, "byte-identical content yields the same content-addressed id"
    prior_only = _make_chunk(heading_path=("Prior",), ordinal=0, text="only in the prior generation")
    new_only = _make_chunk(
        heading_path=("New",),
        ordinal=0,
        text="only in the new generation",
        conversion_output_id=_OUTPUT_B,
        markdown_hash=_MARKDOWN_HASH_B,
    )

    persist_chunks(
        session,
        source_id=_AIZK_UUID,
        conversion_output_id=_OUTPUT,
        markdown_hash_xx64=_MARKDOWN_HASH,
        splitter_version=SPLITTER_VERSION,
        chunks=[shared, prior_only],
    )
    session.commit()

    persist_chunks(
        session,
        source_id=_AIZK_UUID,
        conversion_output_id=_OUTPUT_B,
        markdown_hash_xx64=_MARKDOWN_HASH_B,
        splitter_version=SPLITTER_VERSION,
        chunks=[shared_rechunked, new_only],
    )
    session.commit()
    return shared, prior_only, new_only


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
    assert (row.text, row.content_hash, row.source_id) == (shared.text, shared.content_hash, shared.source_id)
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
    first = persist_chunks(
        session,
        source_id=_AIZK_UUID,
        conversion_output_id=_OUTPUT,
        markdown_hash_xx64=_MARKDOWN_HASH,
        splitter_version=SPLITTER_VERSION,
        chunks=[chunk],
    )
    session.commit()
    manifest_after_first = len(session.exec(select(ChunkRunManifest)).all())

    second = persist_chunks(
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
    """A splitter_version bump opens a superseding run although the content (chunk_id) is unchanged."""
    chunk_v1 = _make_chunk(text="unchanged content", splitter_version=SPLITTER_VERSION)
    first = persist_chunks(
        session,
        source_id=_AIZK_UUID,
        conversion_output_id=_OUTPUT,
        markdown_hash_xx64=_MARKDOWN_HASH,
        splitter_version=SPLITTER_VERSION,
        chunks=[chunk_v1],
    )
    session.commit()

    chunk_v2 = _make_chunk(text="unchanged content", splitter_version=SPLITTER_VERSION + 1)
    assert chunk_v2.chunk_id == chunk_v1.chunk_id, "chunk_id is content-addressed and ignores splitter_version"

    second = persist_chunks(
        session,
        source_id=_AIZK_UUID,
        conversion_output_id=_OUTPUT,
        markdown_hash_xx64=_MARKDOWN_HASH,
        splitter_version=SPLITTER_VERSION + 1,
        chunks=[chunk_v2],
    )
    session.commit()

    assert second.id != first.id
    statuses = {
        r.id: r.status for r in session.exec(select(PipelineRun).where(PipelineRun.scope_id == _AIZK_UUID)).all()
    }
    assert statuses == {first.id: RunStatus.SUPERSEDED, second.id: RunStatus.ACTIVE}


# --------------------------------------------------------------------------- #
# Provenance guard: chunk fields must match the run keyed by them
# --------------------------------------------------------------------------- #


def test_reuse_requires_matching_spans_not_just_ids(session: Session) -> None:
    """The unchanged-run fast path compares the full manifest (chunk_id + span), not ids alone.

    Re-presenting the same content-addressed chunk_id under the same derivation key
    but a different span must not be silently absorbed into the active run as if
    unchanged; the manifest comparison includes the span.
    """
    chunk = _make_chunk(text="stable content")
    first = persist_chunks(
        session,
        source_id=_AIZK_UUID,
        conversion_output_id=_OUTPUT,
        markdown_hash_xx64=_MARKDOWN_HASH,
        splitter_version=SPLITTER_VERSION,
        chunks=[chunk],
    )
    session.commit()

    # Same chunk_id and stable facts (span is not a stable fact), but a shifted span.
    shifted = chunk.model_copy(update={"span": (5, 5 + len(chunk.text))})
    assert shifted.chunk_id == chunk.chunk_id
    second = persist_chunks(
        session,
        source_id=_AIZK_UUID,
        conversion_output_id=_OUTPUT,
        markdown_hash_xx64=_MARKDOWN_HASH,
        splitter_version=SPLITTER_VERSION,
        chunks=[shifted],
    )
    session.commit()

    assert second.id != first.id, "a span change is not treated as an unchanged rerun"
    statuses = {
        r.id: r.status for r in session.exec(select(PipelineRun).where(PipelineRun.scope_id == _AIZK_UUID)).all()
    }
    assert statuses == {first.id: RunStatus.SUPERSEDED, second.id: RunStatus.ACTIVE}


def test_persist_rejects_conflicting_stable_facts_for_existing_chunk_id(session: Session) -> None:
    """An incoming chunk reusing an existing chunk_id with different stable facts is rejected.

    Guards the content-addressed invariant against a hash collision or a caller
    that fabricates a colliding id; reusing the row would repoint the manifest at
    the wrong source text.
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

    # A fabricated chunk reusing original's id but carrying different text; a new
    # generation (new markdown hash) forces the write path, not the reuse fast path.
    colliding = SplitterChunk(
        chunk_id=original.chunk_id,
        content_hash=xxhash.xxh64(b"different content").hexdigest(),
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

    with pytest.raises(ValueError, match="different stable identity facts"):
        persist_chunks(
            session,
            source_id=_AIZK_UUID,
            conversion_output_id=_OUTPUT_B,
            markdown_hash_xx64=_MARKDOWN_HASH_B,
            splitter_version=SPLITTER_VERSION,
            chunks=[colliding],
        )
    # The existing identity is unmodified and no second generation was opened.
    row = session.get(Chunk, original.chunk_id)
    assert row is not None and row.text == "original content"
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
