"""Behavioral tests for the FTS5 content index (``graph_content_fts``).

Covers the search **data foundation**: the migration that creates and backfills
the index (and its FTS5-availability guard), the rebuild routine that reconstructs
it from the source tables, and the two append-only live inserts in the persist
path. "Searchable" here means a row returned by a direct
``SELECT ... FROM graph_content_fts WHERE graph_content_fts MATCH ?`` — the ranked
provider query is a separate concern. Tests assert observable index contents, not
internal call shapes.
"""

from __future__ import annotations

from collections.abc import Callable
import importlib.util
from pathlib import Path
from uuid import UUID

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlmodel import Session
import xxhash

from aizk.chunking import SPLITTER_VERSION, Chunk as SplitterChunk
from aizk.db.migrations.versions.fc430997788f_baseline import _assert_fts5_available
from aizk.graph.content_index import index_chunk_content, index_contextualized_content, rebuild_content_index
from aizk.graph.contextualization import contextualize_chunks, summarize_document
from aizk.graph.llm import STUB_MODEL_CONFIG, StubLLMClient
from aizk.graph.persistence import persist_chunks
from aizk.pipeline.identity import content_fingerprint

_AIZK_UUID = UUID("11111111-1111-1111-1111-111111111111")
_AIZK_UUID_B = UUID("22222222-2222-2222-2222-222222222222")
_OUTPUT = "output-1"
_HASH_A = "0011223344556677"
_HASH_B = "aabbccddeeff0011"
_DOC_TEXT = "# Title\n\nThe document body the summary pass reads."

_MIGRATIONS_DIR = Path(importlib.util.find_spec("aizk.db.migrations").origin).resolve().parent


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _make_chunk(
    text_: str,
    *,
    ordinal: int,
    source_id: UUID = _AIZK_UUID,
    markdown_hash: str = _HASH_A,
    conversion_output_id: str = _OUTPUT,
    splitter_version: int = SPLITTER_VERSION,
) -> SplitterChunk:
    """Build a splitter chunk for a single source (identity is assigned at persistence)."""
    content_hash = xxhash.xxh64(text_.encode("utf-8")).hexdigest()
    return SplitterChunk(
        content_hash=content_hash,
        source_id=str(source_id),
        heading_path=(),
        ordinal=ordinal,
        text=text_,
        char_count=len(text_),
        converted_artifact_id=conversion_output_id,
        markdown_hash_xx64=markdown_hash,
        span=(0, len(text_)),
        splitter_version=splitter_version,
    )


def _search(session: Session, term: str, *, kind: str | None = None) -> list[tuple[str, str]]:
    """Return ``(kind, chunk_id)`` rows matching ``term`` in the index, optional kind filter."""
    sql = "SELECT kind, chunk_id FROM graph_content_fts WHERE graph_content_fts MATCH :term"
    params: dict[str, object] = {"term": term}
    if kind is not None:
        sql += " AND kind = :kind"
        params["kind"] = kind
    return [(row[0], row[1]) for row in session.connection().execute(text(sql), params).all()]


def _alembic_cfg(database_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def _seed_chunk_and_variant(
    session: Session,
    *,
    source_id: UUID,
    chunk_text: str,
    revision: str,
    ordinal: int = 0,
    markdown_hash: str = _HASH_A,
) -> SplitterChunk:
    """Persist one chunk and one contextualized variant; return the surrogate-bearing chunk.

    ``revision`` is the model output: an empty string means a self-contained chunk
    (whose contextualized representation is the raw text). Commits.
    """
    chunk = _make_chunk(chunk_text, ordinal=ordinal, source_id=source_id, markdown_hash=markdown_hash)
    chunking_run, persisted = persist_chunks(
        session,
        source_id=source_id,
        conversion_output_id=_OUTPUT,
        markdown_hash_xx64=markdown_hash,
        markdown_fingerprint=content_fingerprint(markdown_hash),
        splitter_version=SPLITTER_VERSION,
        chunks=[chunk],
    )
    chunk_p = persisted[0]
    summary = summarize_document(
        session,
        StubLLMClient(),
        source_id=source_id,
        conversion_output_id=_OUTPUT,
        markdown_hash_xx64=markdown_hash,
        document_text=_DOC_TEXT,
        model_config=STUB_MODEL_CONFIG,
    )
    assert chunking_run.id is not None
    contextualize_chunks(
        session,
        StubLLMClient(responder=lambda _prompt: revision),
        source_id=source_id,
        summary=summary,
        chunks=[chunk_p],
        chunking_run_id=chunking_run.id,
        splitter_version=SPLITTER_VERSION,
        model_config=STUB_MODEL_CONFIG,
        precomputed_revisions=[revision],
    )
    session.commit()
    return chunk_p


# --------------------------------------------------------------------------- #
# Migration + availability check
# --------------------------------------------------------------------------- #


def test_migration_upgrade_downgrade_round_trips_cleanly(tmp_path: Path) -> None:
    """Upgrading to head creates ``graph_content_fts``; downgrading to base drops it (with shadows)."""
    url = f"sqlite:///{tmp_path / 'fts_round_trip.db'}"
    cfg = _alembic_cfg(url)

    command.upgrade(cfg, "head")
    tables_at_head = set(inspect(create_engine(url)).get_table_names())
    assert "graph_content_fts" in tables_at_head

    command.downgrade(cfg, "base")
    tables_after = set(inspect(create_engine(url)).get_table_names())
    # DROP TABLE on the virtual table removes its shadow tables too — none linger.
    assert not any(t.startswith("graph_content_fts") for t in tables_after)


def test_availability_check_raises_when_fts5_unavailable() -> None:
    """The guard raises a clear, FTS5-naming error when the probe cannot be created.

    Exercises the negative branch (FTS5 IS available in this build) by passing a
    fake bind whose ``execute`` raises, simulating a SQLite build without FTS5.
    """

    class _NoFts5Bind:
        def execute(self, _statement: object) -> None:
            raise RuntimeError("no such module: fts5")

    with pytest.raises(RuntimeError, match="FTS5"):
        _assert_fts5_available(_NoFts5Bind())


def test_availability_check_passes_on_real_fts5_build(tmp_path: Path) -> None:
    """The guard returns without error against this environment's FTS5-enabled SQLite."""
    engine = create_engine(f"sqlite:///{tmp_path / 'probe.db'}")
    with engine.begin() as conn:
        _assert_fts5_available(conn)  # does not raise


# --------------------------------------------------------------------------- #
# Rebuild — superseded-then-reactivated content
# --------------------------------------------------------------------------- #


def test_rebuild_indexes_superseded_then_reactivated_chunk(session: Session) -> None:
    """A chunk superseded when the index was built but reused in a later active run is searchable.

    The rebuild indexes *all* committed chunks, not just the active generation, so
    a surrogate ``chunk_id`` that is reused (not re-created) by a later active run is
    in the index after a rebuild regardless of which run is current.
    """
    target = _make_chunk("quasar luminosity spectra", ordinal=0)
    _run1, persisted1 = persist_chunks(
        session,
        source_id=_AIZK_UUID,
        conversion_output_id=_OUTPUT,
        markdown_hash_xx64=_HASH_A,
        markdown_fingerprint=content_fingerprint(_HASH_A),
        splitter_version=SPLITTER_VERSION,
        chunks=[target],
    )
    session.commit()
    target_id = persisted1[0].chunk_id
    # Generation 2 under a different markdown supersedes run 1, but the manifest
    # set differs (an extra chunk), so run 1 is demoted to superseded. The target
    # chunk_id is reused (not re-created), so its only chunk row is from run 1.
    other = _make_chunk("unrelated body text", ordinal=1, markdown_hash=_HASH_B)
    target_in_gen2 = _make_chunk("quasar luminosity spectra", ordinal=0, markdown_hash=_HASH_B)
    _run2, persisted2 = persist_chunks(
        session,
        source_id=_AIZK_UUID,
        conversion_output_id=_OUTPUT,
        markdown_hash_xx64=_HASH_B,
        markdown_fingerprint=content_fingerprint(_HASH_B),
        splitter_version=SPLITTER_VERSION,
        chunks=[target_in_gen2, other],
    )
    session.commit()
    # Byte-identical content reuses the surrogate identity across generations: a
    # single immutable chunk row, not a duplicate per generation.
    assert persisted2[0].chunk_id == target_id

    # Rebuild from source tables (the all-committed-rows backfill) after both
    # generations exist; the contract is that the reused chunk's *content* is
    # reproduced in the rebuilt index regardless of run currency.
    rebuild_content_index(session.connection())
    session.commit()
    indexed_chunk_ids = {chunk_id for kind, chunk_id in _search(session, "quasar") if kind == "chunk"}
    row_id_to_text = {
        row[0]: row[1] for row in session.connection().execute(text("SELECT chunk_id, text FROM graph_chunks")).all()
    }
    # Exactly the reused chunk's content is searchable as kind=chunk (one row, not a
    # per-generation duplicate), and the indexed id resolves to that content.
    assert {row_id_to_text[cid] for cid in indexed_chunk_ids} == {"quasar luminosity spectra"}


# --------------------------------------------------------------------------- #
# Rebuild
# --------------------------------------------------------------------------- #


def test_rebuild_reproduces_content(session: Session) -> None:
    """Rebuilding from the source tables reproduces the same searchable content.

    Guards the intentional SQL duplication between the migration backfill and the
    app rebuild routine: after persisting via the live inserts, clearing and
    rebuilding from source tables yields the same searchable rows by ``chunk_id``.
    """
    chunk = _seed_chunk_and_variant(
        session,
        source_id=_AIZK_UUID,
        chunk_text="photosynthesis converts light into chemical energy",
        revision="plants use photosynthesis to convert sunlight into chemical energy",
    )

    before = sorted(_search(session, "photosynthesis"))
    rebuild_content_index(session.connection())
    session.commit()
    after = sorted(_search(session, "photosynthesis"))

    assert before == after, "rebuild reproduces the same searchable rows"
    assert {kind for kind, _ in after} == {"chunk", "contextualized"}
    assert all(UUID(chunk_id) == UUID(chunk.chunk_id) for _, chunk_id in after)


def test_rebuild_indexes_the_scope_key_form_the_search_join_uses(session: Session) -> None:
    """A rebuilt row's ``scope_id`` equals the chunking run's ``scope_id``.

    Both columns bind through the ORM ``Uuid`` type and the rebuild copies
    ``graph_chunks.source_id`` verbatim, so the two stored (dashless hex) values
    are compared directly. A regression here costs no error — the join just stops
    matching — so the two are compared directly rather than trusting no exception.
    """
    _seed_chunk_and_variant(
        session,
        source_id=_AIZK_UUID,
        chunk_text="tardigrades survive vacuum exposure",
        revision="",
    )

    rebuild_content_index(session.connection())
    session.commit()

    indexed = {
        row[0] for row in session.connection().execute(text("SELECT DISTINCT scope_id FROM graph_content_fts")).all()
    }
    scopes = {
        row[0]
        for row in session.connection()
        .execute(text("SELECT DISTINCT scope_id FROM pipeline_runs WHERE stage = 'chunking'"))
        .all()
    }
    assert indexed == scopes == {_AIZK_UUID.hex}


@pytest.mark.parametrize(
    ("stored", "reason"),
    [
        ("11111111-1111-1111-1111-111111111111", "dashed"),
        ("1111111111111111111111111111111", "too short"),
        ("0F8CD1A200344B0080CD000000000ABC", "uppercase"),
    ],
    ids=["dashed", "too-short", "uppercase"],
)
def test_rebuild_refuses_a_chunk_identity_outside_the_storage_form(session: Session, stored: str, reason: str) -> None:
    """A stored identity the scope-key rendering would mangle stops the rebuild.

    The rendering slices at fixed offsets, so each of these would produce a
    plausible-looking scope key matching no run. Uppercase is included because the
    join is case-sensitive.
    """
    _seed_chunk_and_variant(
        session,
        source_id=_AIZK_UUID,
        chunk_text="a chunk whose identity is about to be corrupted",
        revision="",
    )
    session.connection().execute(text("UPDATE graph_chunks SET source_id = :stored"), {"stored": stored})

    with pytest.raises(ValueError, match="32 lowercase hex characters"):
        rebuild_content_index(session.connection())


# --------------------------------------------------------------------------- #
# Live write-sites
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "indexer",
    [index_chunk_content, index_contextualized_content],
    ids=["chunk", "contextualized"],
)
@pytest.mark.parametrize(
    "scope_id",
    [
        "11111111-1111-1111-1111-111111111111",
        "11111111111111111111111111111111",
        1,
        None,
    ],
    ids=["dashed-string", "dashless-string", "int", "none"],
)
def test_live_insert_refuses_a_scope_key_the_search_join_could_not_match(
    session: Session,
    indexer: Callable[..., None],
    scope_id: object,
) -> None:
    """A scope value that is not a ``uuid.UUID`` instance is refused at the insert.

    The index's ``scope_id`` binds through the same ORM ``Uuid`` type as
    ``pipeline_runs.scope_id``. Any other Python type would bind incorrectly (or
    not at all) and match no run, leaving the content committed but unfindable, so
    both live inserts reject it rather than write it.
    """
    with pytest.raises(TypeError, match="content index would match no run"):
        indexer(session, text_="some indexable text", chunk_id="chunk-1", run_id=None, scope_id=scope_id)


@pytest.mark.parametrize(
    "indexer",
    [index_chunk_content, index_contextualized_content],
    ids=["chunk", "contextualized"],
)
@pytest.mark.parametrize(
    "chunk_id",
    [
        "22222222-2222-2222-2222-222222222222",
        "22222222222222222222222222222222",
        1,
        None,
    ],
    ids=["dashed-string", "dashless-string", "int", "none"],
)
def test_live_insert_refuses_a_chunk_identity_the_manifest_join_could_not_match(
    session: Session,
    indexer: Callable[..., None],
    chunk_id: object,
) -> None:
    """A chunk value that is not a ``uuid.UUID`` instance is refused at the insert.

    The index's ``chunk_id`` binds through the same ORM ``Uuid`` type as
    ``graph_chunk_run_manifest.chunk_id``. Any other Python type would bind
    incorrectly (or not at all) and match no manifest entry, leaving the content
    committed but unfindable, so both live inserts reject it rather than write it.
    """
    with pytest.raises(TypeError, match="content index would match no manifest entry"):
        indexer(session, text_="some indexable text", chunk_id=chunk_id, run_id=None, scope_id=_AIZK_UUID)


@pytest.mark.parametrize(
    "indexer",
    [index_chunk_content, index_contextualized_content],
    ids=["chunk", "contextualized"],
)
def test_live_insert_accepts_the_canonical_scope_key(session: Session, indexer: Callable[..., None]) -> None:
    """A ``UUID`` scope is indexed in the same storage form ``pipeline_runs.scope_id`` holds.

    Persisting a real chunking run for the same source and joining the two tables
    on ``scope_id`` — rather than just observing that the insert did not raise —
    proves the search join would actually match.
    """
    probe_chunk_id = UUID("33333333-3333-3333-3333-333333333333")
    indexer(session, text_="some indexable text", chunk_id=probe_chunk_id, run_id=None, scope_id=_AIZK_UUID)
    persist_chunks(
        session,
        source_id=_AIZK_UUID,
        conversion_output_id=_OUTPUT,
        markdown_hash_xx64=_HASH_A,
        markdown_fingerprint=content_fingerprint(_HASH_A),
        splitter_version=SPLITTER_VERSION,
        chunks=[_make_chunk("distinct join probe text", ordinal=0)],
    )
    session.commit()

    joined = (
        session.connection()
        .execute(
            text(
                "SELECT count(*) FROM graph_content_fts AS f "
                "JOIN pipeline_runs AS cr ON cr.stage = 'chunking' AND cr.scope_id = f.scope_id "
                "WHERE cr.status = 'active' AND f.chunk_id = :chunk_id"
            ),
            {"chunk_id": probe_chunk_id.hex},
        )
        .scalar()
    )
    assert joined == 1, "the live-inserted scope_id matches the stored pipeline_runs.scope_id form"


def test_persisted_chunk_is_searchable(session: Session) -> None:
    """A newly persisted chunk is indexed and searchable by its raw text."""
    chunk = _make_chunk("mitochondria are the powerhouse of the cell", ordinal=0)
    _run, persisted = persist_chunks(
        session,
        source_id=_AIZK_UUID,
        conversion_output_id=_OUTPUT,
        markdown_hash_xx64=_HASH_A,
        markdown_fingerprint=content_fingerprint(_HASH_A),
        splitter_version=SPLITTER_VERSION,
        chunks=[chunk],
    )
    session.commit()

    hits = _search(session, "mitochondria")
    assert hits == [("chunk", UUID(persisted[0].chunk_id).hex)], "the new chunk's raw text is searchable as kind=chunk"


def test_reused_chunk_id_is_not_indexed_twice(session: Session) -> None:
    """Re-persisting an unchanged chunk does not add a second chunk index row.

    A reused ``chunk_id`` is not re-created in ``graph_chunks``, so it must not be
    re-inserted into the index — one row per chunk-row creation.
    """
    chunk = _make_chunk("the distinctive antidisestablishment token", ordinal=0)
    args = {
        "source_id": _AIZK_UUID,
        "conversion_output_id": _OUTPUT,
        "markdown_hash_xx64": _HASH_A,
        "markdown_fingerprint": content_fingerprint(_HASH_A),
        "splitter_version": SPLITTER_VERSION,
        "chunks": [chunk],
    }
    _run, persisted = persist_chunks(session, **args)
    session.commit()
    # Identical inputs: persist_chunks reuses the active run and re-creates nothing.
    persist_chunks(session, **args)
    session.commit()

    hits = _search(session, "antidisestablishment", kind="chunk")
    assert hits == [("chunk", UUID(persisted[0].chunk_id).hex)], "exactly one chunk index row despite the re-persist"


def test_persisted_variant_is_searchable(session: Session) -> None:
    """A newly persisted variant's revision is searchable under kind=contextualized."""
    _seed_chunk_and_variant(
        session,
        source_id=_AIZK_UUID,
        chunk_text="it improves throughput",
        revision="scaled dot-product attention improves throughput",
    )
    # The term appears only in the revision, not the raw chunk text.
    contextualized = _search(session, "scaled", kind="contextualized")
    raw = _search(session, "scaled", kind="chunk")
    assert len(contextualized) == 1, "the revision is searchable as kind=contextualized"
    assert raw == [], "the term introduced by contextualization is not in the raw chunk index"


def test_self_contained_chunk_indexes_raw_text_as_contextualized(session: Session) -> None:
    """A self-contained chunk (empty revision) is searchable by its raw text under kind=contextualized."""
    chunk = _seed_chunk_and_variant(
        session,
        source_id=_AIZK_UUID,
        chunk_text="bioluminescence in deep-sea organisms",
        revision="",  # empty revision => self-contained
    )
    hits = _search(session, "bioluminescence", kind="contextualized")
    assert hits == [("contextualized", UUID(chunk.chunk_id).hex)], (
        "the self-contained chunk's contextualized representation is its raw text"
    )


def test_reused_variant_run_does_not_reindex(session: Session) -> None:
    """Re-contextualizing with unchanged inputs reuses the active run and adds no index row.

    The reuse early-return path must not insert; only the newly-recorded run path
    indexes, so a self-contained chunk keeps exactly one contextualized index row.
    """
    chunk = _make_chunk("a stable self-contained passage", ordinal=0)
    chunking_run, persisted = persist_chunks(
        session,
        source_id=_AIZK_UUID,
        conversion_output_id=_OUTPUT,
        markdown_hash_xx64=_HASH_A,
        markdown_fingerprint=content_fingerprint(_HASH_A),
        splitter_version=SPLITTER_VERSION,
        chunks=[chunk],
    )
    chunk_p = persisted[0]
    session.commit()
    assert chunking_run.id is not None
    summary = summarize_document(
        session,
        StubLLMClient(),
        source_id=_AIZK_UUID,
        conversion_output_id=_OUTPUT,
        markdown_hash_xx64=_HASH_A,
        document_text=_DOC_TEXT,
        model_config=STUB_MODEL_CONFIG,
    )

    def _contextualize() -> None:
        contextualize_chunks(
            session,
            StubLLMClient(responder=lambda _prompt: ""),
            source_id=_AIZK_UUID,
            summary=summary,
            chunks=[chunk_p],
            chunking_run_id=chunking_run.id,
            splitter_version=SPLITTER_VERSION,
            model_config=STUB_MODEL_CONFIG,
            precomputed_revisions=[""],
        )
        session.commit()

    _contextualize()
    _contextualize()  # unchanged inputs => active run reused, no new variant, no new index row

    hits = _search(session, "passage", kind="contextualized")
    assert hits == [("contextualized", UUID(chunk_p.chunk_id).hex)], "exactly one contextualized index row after reuse"


def test_index_excludes_other_source(session: Session) -> None:
    """A term is scoped to the source whose content contains it (source_id discrimination)."""
    chunk_a = _make_chunk("alpha distinctive term", ordinal=0, source_id=_AIZK_UUID)
    _run_a, persisted_a = persist_chunks(
        session,
        source_id=_AIZK_UUID,
        conversion_output_id=_OUTPUT,
        markdown_hash_xx64=_HASH_A,
        markdown_fingerprint=content_fingerprint(_HASH_A),
        splitter_version=SPLITTER_VERSION,
        chunks=[chunk_a],
    )
    chunk_a_p = persisted_a[0]
    chunk_b = _make_chunk("beta distinctive term", ordinal=0, source_id=_AIZK_UUID_B)
    persist_chunks(
        session,
        source_id=_AIZK_UUID_B,
        conversion_output_id=_OUTPUT,
        markdown_hash_xx64=_HASH_A,
        markdown_fingerprint=content_fingerprint(_HASH_A),
        splitter_version=SPLITTER_VERSION,
        chunks=[chunk_b],
    )
    session.commit()

    rows = (
        session.connection()
        .execute(
            text("SELECT scope_id, chunk_id FROM graph_content_fts WHERE graph_content_fts MATCH :t"),
            {"t": "alpha"},
        )
        .all()
    )
    assert [(r[0], r[1]) for r in rows] == [(_AIZK_UUID.hex, UUID(chunk_a_p.chunk_id).hex)]
