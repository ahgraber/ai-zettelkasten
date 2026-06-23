"""Referential-integrity test for the chunk_id surrogate migration on populated data.

The surrogate migration assigns a UUID ``chunk_id`` per existing chunk row and
repoints every reference (chunk-run manifest, contextualized variants, and the
``graph_content_fts`` content index) through the old→new map. This seeds a
populated database at the pre-surrogate revision with content-addressed
``chunk_id``s and asserts that, after upgrading across the surrogate step, every
reference resolves to a chunk row with no dangling reference and the identities are
now surrogates (UUIDs), not the prior content hashes.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from uuid import UUID

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from aizk.chunking.datamodel import derive_chunk_content_key

_MIGRATIONS_DIR = Path(importlib.util.find_spec("aizk.db.migrations").origin).resolve().parent

# The revision immediately before the surrogate-and-rename revision under test.
_PRE_SURROGATE_REVISION = "d3e4f5a6b7c8"
_SURROGATE_REVISION = "f1a2b3c4d5e6"

_SOURCE_ID = "11111111-1111-1111-1111-111111111111"


def _alembic_cfg(database_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def _old_chunk_id(heading_path: tuple[str, ...], ordinal: int, content_hash: str) -> str:
    """The pre-surrogate content-addressed chunk_id for a row (what the fixture seeds)."""
    return derive_chunk_content_key(_SOURCE_ID, heading_path, ordinal, content_hash)


def test_fk_integrity_after_repoint(tmp_path: Path) -> None:
    """After the surrogate migration, every repointed chunk_id reference resolves; ids are UUIDs."""
    url = f"sqlite:///{tmp_path / 'repoint.db'}"
    cfg = _alembic_cfg(url)
    engine = create_engine(url)

    # Seed a populated database at the pre-surrogate revision (graph_chunks still
    # has the ``doc_id`` column and content-addressed ``chunk_id`` PKs).
    command.upgrade(cfg, _PRE_SURROGATE_REVISION)

    # Three chunks; one is referenced by a contextualized variant.
    fixtures = [
        {"heading_path": "[]", "ordinal": 0, "content_hash": "00aa00aa00aa00aa", "text": "first chunk text"},
        {"heading_path": '["Section"]', "ordinal": 0, "content_hash": "11bb11bb11bb11bb", "text": "second chunk text"},
        {"heading_path": '["Section"]', "ordinal": 1, "content_hash": "22cc22cc22cc22cc", "text": "third chunk text"},
    ]
    old_ids = [_old_chunk_id(tuple(json.loads(f["heading_path"])), f["ordinal"], f["content_hash"]) for f in fixtures]

    with engine.begin() as conn:
        for fixture, old_id in zip(fixtures, old_ids, strict=True):
            conn.execute(
                text(
                    "INSERT INTO graph_chunks (chunk_id, content_hash, doc_id, heading_path_json, ordinal, text, char_count) "
                    "VALUES (:chunk_id, :content_hash, :doc_id, :heading_path, :ordinal, :text, :char_count)"
                ),
                {
                    "chunk_id": old_id,
                    "content_hash": fixture["content_hash"],
                    "doc_id": _SOURCE_ID,
                    "heading_path": fixture["heading_path"],
                    "ordinal": fixture["ordinal"],
                    "text": fixture["text"],
                    "char_count": len(fixture["text"]),
                },
            )
            # Each chunk appears in a chunking run manifest.
            conn.execute(
                text(
                    "INSERT INTO graph_chunk_run_manifest (run_id, chunk_id, span_start, span_end) "
                    "VALUES (:run_id, :chunk_id, :span_start, :span_end)"
                ),
                {"run_id": 1, "chunk_id": old_id, "span_start": 0, "span_end": len(fixture["text"])},
            )
        # A contextualized variant references the first chunk by chunk_id.
        conn.execute(
            text(
                "INSERT INTO graph_contextualized_chunks "
                "(run_id, summary_run_id, chunking_run_id, chunk_id, context_version, contextualized_text, derivation_key, created_at) "
                "VALUES (2, 3, 1, :chunk_id, 1, :ctx, :key, :now)"
            ),
            {"chunk_id": old_ids[0], "ctx": "contextualized first", "key": '{"k":"v"}', "now": "2026-06-06T00:00:00"},
        )

    # Upgrade across the surrogate step (and the source_id rename).
    command.upgrade(cfg, _SURROGATE_REVISION)

    with engine.begin() as conn:
        chunk_ids = {row[0] for row in conn.execute(text("SELECT chunk_id FROM graph_chunks")).fetchall()}
        assert len(chunk_ids) == len(fixtures), "every chunk row is preserved"
        # Identities are now surrogates (UUIDs), not the prior content hashes.
        for chunk_id in chunk_ids:
            assert UUID(chunk_id), f"{chunk_id!r} is not a UUID surrogate"
        assert chunk_ids.isdisjoint(set(old_ids)), "no content-addressed id survives as an identity"

        # Every manifest reference resolves to a chunk row (no dangling).
        manifest_ids = {
            row[0] for row in conn.execute(text("SELECT chunk_id FROM graph_chunk_run_manifest")).fetchall()
        }
        assert manifest_ids <= chunk_ids, "manifest chunk_ids repoint to existing chunk rows"
        assert len(manifest_ids) == len(fixtures)

        # Every contextualized-variant reference resolves to a chunk row.
        variant_ids = {
            row[0] for row in conn.execute(text("SELECT chunk_id FROM graph_contextualized_chunks")).fetchall()
        }
        assert variant_ids <= chunk_ids, "variant chunk_ids repoint to existing chunk rows"
        assert len(variant_ids) == 1

        # The rebuilt FTS index references only existing chunk rows (chunk + contextualized).
        fts_ids = {row[0] for row in conn.execute(text("SELECT chunk_id FROM graph_content_fts")).fetchall()}
        assert fts_ids, "the FTS index was rebuilt with content"
        assert fts_ids <= chunk_ids, "FTS chunk_ids repoint to existing chunk rows"

    engine.dispose()
