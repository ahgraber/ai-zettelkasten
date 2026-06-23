"""Surrogate chunk identity and sameness-key reuse at persistence.

Exercises the ``chunking`` delta's identity contract: ``chunk_id`` is a stable
surrogate assigned once at persistence (a UUID, never content-derived), reused
across generations exactly when the sameness-key ``(source_id, heading_path,
ordinal, content_hash)`` matches and minted anew otherwise. The three identity
scenarios (same address + same content, same address + different content,
different address + same content) and the reuse/novel guarantees are asserted on
the persisted surrogate, with ``content_hash`` surviving as a separate observable
column so a consumer can still tell an address change from a content change.
"""

from __future__ import annotations

from uuid import UUID

from sqlmodel import Session, select
import xxhash

from aizk.chunking import SPLITTER_VERSION, Chunk as SplitterChunk
from aizk.graph.datamodel import Chunk
from aizk.graph.persistence import persist_chunks

_SOURCE_ID = "11111111-1111-1111-1111-111111111111"
_OUTPUT_A = "output-a"
_OUTPUT_B = "output-b"
_HASH_A = "0011223344556677"
_HASH_B = "aabbccddeeff0011"


def _make_chunk(
    *,
    text: str,
    heading_path: tuple[str, ...] = ("Section",),
    ordinal: int = 0,
    source_id: str = _SOURCE_ID,
    conversion_output_id: str = _OUTPUT_A,
    markdown_hash: str = _HASH_A,
) -> SplitterChunk:
    """Build a splitter chunk (no chunk_id — identity is assigned at persistence)."""
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
        splitter_version=SPLITTER_VERSION,
    )


def _persist(
    session: Session,
    chunks: list[SplitterChunk],
    *,
    conversion_output_id: str = _OUTPUT_A,
    markdown_hash: str = _HASH_A,
) -> list[SplitterChunk]:
    """Persist a chunk set and return the chunks carrying their assigned surrogate ``chunk_id``."""
    _run, persisted = persist_chunks(
        session,
        source_id=_SOURCE_ID,
        conversion_output_id=conversion_output_id,
        markdown_hash_xx64=markdown_hash,
        splitter_version=SPLITTER_VERSION,
        chunks=chunks,
    )
    session.commit()
    return persisted


def test_same_address_same_content_same_id(session: Session) -> None:
    """A chunk re-emitted at the same address with identical content reuses its surrogate."""
    gen_a = _persist(session, [_make_chunk(text="unchanged section text")])
    # A later generation (new markdown) re-emits the byte-identical section.
    gen_b = _persist(
        session,
        [_make_chunk(text="unchanged section text", conversion_output_id=_OUTPUT_B, markdown_hash=_HASH_B)],
        conversion_output_id=_OUTPUT_B,
        markdown_hash=_HASH_B,
    )

    assert gen_a[0].chunk_id == gen_b[0].chunk_id, "an identical sameness-key reuses the surrogate identity"
    assert gen_a[0].content_hash == gen_b[0].content_hash
    assert len(session.exec(select(Chunk)).all()) == 1, "the shared chunk is a single row across generations"


def test_same_address_diff_content_diff_id(session: Session) -> None:
    """At the same address, different content yields a different surrogate and content_hash."""
    gen_a = _persist(session, [_make_chunk(text="the original wording")])
    gen_b = _persist(
        session,
        [_make_chunk(text="the edited wording", conversion_output_id=_OUTPUT_B, markdown_hash=_HASH_B)],
        conversion_output_id=_OUTPUT_B,
        markdown_hash=_HASH_B,
    )

    assert gen_a[0].chunk_id != gen_b[0].chunk_id
    assert gen_a[0].content_hash != gen_b[0].content_hash
    assert len(session.exec(select(Chunk)).all()) == 2, "the edited content is a distinct identity"


def test_diff_address_same_content_diff_id(session: Session) -> None:
    """Identical content at different addresses yields distinct surrogates but an equal content_hash."""
    persisted = _persist(
        session,
        [
            _make_chunk(text="boilerplate notice", heading_path=("Intro",), ordinal=0),
            _make_chunk(text="boilerplate notice", heading_path=("Appendix",), ordinal=0),
        ],
    )

    intro, appendix = persisted
    assert intro.chunk_id != appendix.chunk_id, "different addresses are different identities"
    assert intro.content_hash == appendix.content_hash, (
        "the content fingerprint is equal (address change, not content)"
    )
    assert len(session.exec(select(Chunk)).all()) == 2


def test_chunk_id_is_a_surrogate_uuid(session: Session) -> None:
    """The assigned ``chunk_id`` is a UUID surrogate, not a content-derived value."""
    persisted = _persist(session, [_make_chunk(text="some content")])

    chunk_id = persisted[0].chunk_id
    assert chunk_id is not None
    # Parses as a UUID and is not the content_hash — identity is assigned, not derived.
    assert UUID(chunk_id)
    assert chunk_id != persisted[0].content_hash


def test_re_persist_reuses_identity(session: Session) -> None:
    """Re-persisting a chunk with an existing sameness-key reuses its surrogate, no duplicate row."""
    first = _persist(session, [_make_chunk(text="stable content")])
    second = _persist(session, [_make_chunk(text="stable content")])

    assert first[0].chunk_id == second[0].chunk_id
    rows = session.exec(select(Chunk).where(Chunk.chunk_id == first[0].chunk_id)).all()
    assert len(rows) == 1, "no duplicate identity for a re-persisted sameness-key"


def test_novel_chunk_stored_once(session: Session) -> None:
    """A chunk whose sameness-key is not present is stored as exactly one new identity."""
    persisted = _persist(session, [_make_chunk(text="a brand new chunk")])

    rows = session.exec(select(Chunk)).all()
    assert len(rows) == 1
    assert rows[0].chunk_id == persisted[0].chunk_id
    assert rows[0].content_hash == persisted[0].content_hash
