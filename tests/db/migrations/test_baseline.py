"""Tests for the single-baseline Alembic migration chain.

Covers the ``schema-migrations`` spec: a fresh database migrates to head in
exactly one revision, the migrated schema is ORM-baseline equivalent
(including the ``graph_content_fts`` full-text index), and a full
upgrade/downgrade round-trip on an empty database leaves no user tables. The
round-trip also stands as the check that the revision's ``downgrade()`` is
implemented: a no-op ``downgrade()`` would leave every table in place.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlmodel import SQLModel

from tests.db.migrations._schema_equivalence import assert_table_schema_equivalent

_MIGRATIONS_DIR = Path(importlib.util.find_spec("aizk.db.migrations").origin).resolve().parent


def _alembic_cfg(database_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def test_fresh_database_upgrades_to_head_in_one_revision(tmp_path: Path) -> None:
    """A fresh database migrates to head by applying exactly one revision."""
    url = f"sqlite:///{tmp_path / 'one_revision.db'}"
    cfg = _alembic_cfg(url)

    script = ScriptDirectory.from_config(cfg)
    revisions = list(script.walk_revisions())
    assert len(revisions) == 1, f"expected a single baseline revision, found {len(revisions)}"

    command.upgrade(cfg, "head")

    with create_engine(url).connect() as conn:
        version_num = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert version_num == script.get_current_head()


def test_upgrade_produces_schema_matching_create_all(tmp_path: Path) -> None:
    """Running the baseline migration produces the same schema as ``create_all``.

    ``command.upgrade`` runs Alembic's ``env.py``, which imports every stage's
    models (conversion, graph, pipeline) onto ``SQLModel.metadata`` — so by the
    time the baseline ``create_all`` call below runs, the full cross-stage
    table set is registered. This is the one parity check that spans every
    table, not just one stage's.
    """
    migrated_url = f"sqlite:///{tmp_path / 'migrated.db'}"
    baseline_url = f"sqlite:///{tmp_path / 'baseline.db'}"

    command.upgrade(_alembic_cfg(migrated_url), "head")

    baseline_engine = create_engine(baseline_url)
    SQLModel.metadata.create_all(baseline_engine)

    migrated_inspector = inspect(create_engine(migrated_url))
    baseline_inspector = inspect(baseline_engine)

    migrated_tables = set(migrated_inspector.get_table_names())
    baseline_tables = set(baseline_inspector.get_table_names())
    migrated_tables.discard("alembic_version")
    # The graph content FTS5 index is a virtual table with auto-created shadow
    # tables (graph_content_fts, _data, _idx, _docsize, _config, _content). None
    # are SQLModel tables, so they cannot appear in the create_all baseline; drop
    # the whole graph_content_fts* family before comparing.
    migrated_tables = {t for t in migrated_tables if not t.startswith("graph_content_fts")}

    assert migrated_tables == baseline_tables, f"Table mismatch: {migrated_tables ^ baseline_tables}"

    for table in baseline_tables:
        assert_table_schema_equivalent(migrated_inspector, baseline_inspector, table)


def test_upgrade_creates_the_content_fts_index(tmp_path: Path) -> None:
    """Upgrading to head creates the ``graph_content_fts`` full-text search table."""
    url = f"sqlite:///{tmp_path / 'fts.db'}"
    command.upgrade(_alembic_cfg(url), "head")

    tables = set(inspect(create_engine(url)).get_table_names())
    assert "graph_content_fts" in tables


def test_upgrade_downgrade_round_trip_leaves_no_user_tables(tmp_path: Path) -> None:
    """A full upgrade-to-head then downgrade-to-base leaves only ``alembic_version``."""
    url = f"sqlite:///{tmp_path / 'roundtrip.db'}"
    cfg = _alembic_cfg(url)

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")

    remaining = set(inspect(create_engine(url)).get_table_names()) - {"alembic_version"}
    assert remaining == set(), f"tables remain after downgrade: {remaining}"


def test_conversion_jobs_source_id_keeps_its_foreign_key(tmp_path: Path) -> None:
    """The migrated ``conversion_jobs.source_id`` references ``sources.source_id``."""
    url = f"sqlite:///{tmp_path / 'conversion_fk.db'}"
    command.upgrade(_alembic_cfg(url), "head")

    foreign_keys = inspect(create_engine(url)).get_foreign_keys("conversion_jobs")
    assert [(fk["constrained_columns"], fk["referred_table"], fk["referred_columns"]) for fk in foreign_keys] == [
        (["source_id"], "sources", ["source_id"])
    ]
