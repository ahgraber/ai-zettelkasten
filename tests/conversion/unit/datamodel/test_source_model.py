"""ORM-layer tests for Source identity-column immutability invariants.

Source identity columns (aizk_uuid, source_ref, source_ref_hash, karakeep_id)
are write-once: the API materializes them at submit time and the worker
must never overwrite them.  These tests verify the ORM column configuration
and DB-level enforcement that guard that contract.
"""

from __future__ import annotations

import datetime
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _engine(tmp_path):
    url = f"sqlite:///{tmp_path}/source_model.db"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return engine


def _source_kwargs(**overrides):
    now = datetime.datetime.now(datetime.timezone.utc)
    defaults = dict(
        karakeep_id=f"k_{uuid4().hex[:8]}",
        source_ref='{"kind":"karakeep_bookmark","bookmark_id":"x"}',
        source_ref_hash=uuid4().hex,
        created_at=now,
        updated_at=now,
    )
    return {**defaults, **overrides}


# ---------------------------------------------------------------------------
# Column-configuration invariants
# ---------------------------------------------------------------------------


def test_identity_columns_have_no_orm_onupdate():
    """Identity columns must carry no ORM onupdate callbacks — they are write-once."""
    from sqlalchemy import inspect as sa_inspect

    from aizk.conversion.datamodel.source import Source

    mapper = sa_inspect(Source)
    identity_cols = {"aizk_uuid", "source_ref", "source_ref_hash", "karakeep_id"}
    for col_name in identity_cols:
        col = mapper.mapper.columns[col_name]
        assert col.onupdate is None, f"Source.{col_name} must not have an ORM onupdate callback (identity column)"


def test_identity_columns_have_no_server_onupdate():
    """Identity columns must carry no server_onupdate expressions."""
    from sqlalchemy import inspect as sa_inspect

    from aizk.conversion.datamodel.source import Source

    mapper = sa_inspect(Source)
    identity_cols = {"aizk_uuid", "source_ref", "source_ref_hash", "karakeep_id"}
    for col_name in identity_cols:
        col = mapper.mapper.columns[col_name]
        assert col.server_onupdate is None, f"Source.{col_name} must not have a server_onupdate expression"


def test_source_ref_hash_has_unique_index():
    """source_ref_hash carries a UNIQUE index enforcing dedup at the DB layer."""
    from sqlalchemy import inspect as sa_inspect

    from aizk.conversion.datamodel.source import Source

    mapper = sa_inspect(Source)
    unique_cols = set()
    for idx in mapper.mapper.persist_selectable.indexes:
        if idx.unique:
            for col in idx.columns:
                unique_cols.add(col.name)

    assert "source_ref_hash" in unique_cols, "source_ref_hash must have a UNIQUE index (dedup enforcement)"


def test_aizk_uuid_has_unique_index():
    """aizk_uuid carries a UNIQUE index — it is the stable FK target for child tables."""
    from sqlalchemy import inspect as sa_inspect

    from aizk.conversion.datamodel.source import Source

    mapper = sa_inspect(Source)
    unique_cols = set()
    for idx in mapper.mapper.persist_selectable.indexes:
        if idx.unique:
            for col in idx.columns:
                unique_cols.add(col.name)

    assert "aizk_uuid" in unique_cols, "aizk_uuid must have a UNIQUE index"


# ---------------------------------------------------------------------------
# DB-level enforcement
# ---------------------------------------------------------------------------


def test_duplicate_source_ref_hash_raises_integrity_error(tmp_path):
    """Inserting two rows with the same source_ref_hash raises IntegrityError."""
    from aizk.conversion.datamodel.source import Source

    engine = _engine(tmp_path)
    shared_hash = uuid4().hex

    with Session(engine) as session:
        session.add(Source(**_source_kwargs(source_ref_hash=shared_hash)))
        session.commit()

    with pytest.raises(IntegrityError), Session(engine) as session:
        session.add(Source(**_source_kwargs(karakeep_id="other_k", source_ref_hash=shared_hash)))
        session.commit()


def test_duplicate_aizk_uuid_raises_integrity_error(tmp_path):
    """Inserting two rows with the same aizk_uuid raises IntegrityError."""
    from aizk.conversion.datamodel.source import Source

    engine = _engine(tmp_path)
    shared_uuid = uuid4()

    with Session(engine) as session:
        session.add(Source(**_source_kwargs(aizk_uuid=shared_uuid, source_ref_hash=uuid4().hex)))
        session.commit()

    with pytest.raises(IntegrityError), Session(engine) as session:
        session.add(
            Source(
                **_source_kwargs(
                    karakeep_id="other_k2",
                    aizk_uuid=shared_uuid,
                    source_ref_hash=uuid4().hex,
                )
            )
        )
        session.commit()


def test_duplicate_karakeep_id_raises_integrity_error(tmp_path):
    """Inserting two rows with the same non-null karakeep_id raises IntegrityError."""
    from aizk.conversion.datamodel.source import Source

    engine = _engine(tmp_path)
    shared_kid = "dup_karakeep"

    with Session(engine) as session:
        session.add(Source(**_source_kwargs(karakeep_id=shared_kid, source_ref_hash=uuid4().hex)))
        session.commit()

    with pytest.raises(IntegrityError), Session(engine) as session:
        session.add(Source(**_source_kwargs(karakeep_id=shared_kid, source_ref_hash=uuid4().hex)))
        session.commit()


def test_multiple_null_karakeep_ids_allowed(tmp_path):
    """Multiple rows with karakeep_id=NULL are allowed (non-KaraKeep sources)."""
    from aizk.conversion.datamodel.source import Source

    engine = _engine(tmp_path)

    with Session(engine) as session:
        for _ in range(3):
            session.add(Source(**_source_kwargs(karakeep_id=None, source_ref_hash=uuid4().hex)))
        session.commit()

    with engine.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM sources WHERE karakeep_id IS NULL")).scalar()
    assert count == 3
