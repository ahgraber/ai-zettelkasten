"""Behavioral tests for the contextualization output-memo repository helpers.

Exercises :func:`aizk.graph.persistence.memo_get`,
:func:`~aizk.graph.persistence.memo_upsert_and_read`, and
:func:`~aizk.graph.persistence.memo_delete_keys` directly against a seeded SQLite
test database. These are the only access path to the
``graph_contextualization_output_memo`` scratch table; the contracts under test are
the three-way present-empty/present-text/absent distinction, the
insert-or-ignore winner semantics (the authoritative stored value is returned),
and key-exact pruning.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlmodel import Session, SQLModel, create_engine

from aizk.graph.db import begin_immediate
from aizk.graph.persistence import memo_delete_keys, memo_get, memo_upsert_and_read

_SUMMARY = "summary"
_REVISION = "revision"
_SCOPE = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """A plain file-based SQLite engine with the full schema.

    Unlike the shared ``engine`` fixture, this one has no auto-``BEGIN IMMEDIATE``
    listener: the memo helpers emit ``BEGIN IMMEDIATE`` themselves, so an
    auto-emitting listener would double-begin. This mirrors production and the
    unit-of-work tests, where ``begin_immediate`` owns the transaction boundary.
    """
    eng = create_engine(
        f"sqlite:///{tmp_path / 'memo.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    SQLModel.metadata.create_all(eng)
    yield eng
    eng.dispose()


def _read(engine: Engine, kind: str, scope_key: str, derivation_key: str) -> str | None:
    """Read a memo value through a short-lived session (no lock held across calls)."""
    with Session(engine) as session:
        return memo_get(session, kind, scope_key, derivation_key)


def test_memo_get_distinguishes_absent_present_empty_and_present_text(engine: Engine) -> None:
    """``memo_get`` returns ``None`` for absent, ``''`` for present-empty, the text otherwise."""
    # Absent → None (a miss; the model must be invoked).
    assert _read(engine, _REVISION, _SCOPE, "key-absent") is None

    # Present-empty ('' is a valid, validated self-contained revision).
    memo_upsert_and_read(engine, _REVISION, _SCOPE, "key-empty", "")
    assert _read(engine, _REVISION, _SCOPE, "key-empty") == ""

    # Present-text.
    memo_upsert_and_read(engine, _REVISION, _SCOPE, "key-text", "a revision")
    assert _read(engine, _REVISION, _SCOPE, "key-text") == "a revision"


def test_memo_upsert_and_read_conflict_returns_authoritative_stored_value(engine: Engine) -> None:
    """On a key conflict the insert is a no-op and the pre-existing value wins, unchanged."""
    first = memo_upsert_and_read(engine, _SUMMARY, _SCOPE, "key", "A")
    assert first == "A"

    # A second upsert of the same key with a different value must not overwrite;
    # it returns the authoritative stored (winner's) value.
    second = memo_upsert_and_read(engine, _SUMMARY, _SCOPE, "key", "B")
    assert second == "A", "ON CONFLICT DO NOTHING keeps the first value; the caller adopts it"
    assert _read(engine, _SUMMARY, _SCOPE, "key") == "A", "the stored row is unchanged"


def test_memo_delete_keys_is_key_exact(engine: Engine) -> None:
    """Deleting listed keys removes only those, leaving other same-scope keys intact."""
    memo_upsert_and_read(engine, _SUMMARY, _SCOPE, "summary-key", "the summary")
    memo_upsert_and_read(engine, _REVISION, _SCOPE, "chunk-1", "rev 1")
    memo_upsert_and_read(engine, _REVISION, _SCOPE, "chunk-2", "rev 2")
    # An unrelated entry under the same scope but a different key must survive.
    memo_upsert_and_read(engine, _REVISION, _SCOPE, "unrelated", "keep me")

    with begin_immediate(engine) as session:
        memo_delete_keys(
            session,
            _SCOPE,
            [(_SUMMARY, "summary-key"), (_REVISION, "chunk-1"), (_REVISION, "chunk-2")],
        )

    assert _read(engine, _SUMMARY, _SCOPE, "summary-key") is None
    assert _read(engine, _REVISION, _SCOPE, "chunk-1") is None
    assert _read(engine, _REVISION, _SCOPE, "chunk-2") is None
    assert _read(engine, _REVISION, _SCOPE, "unrelated") == "keep me", "unlisted same-scope key is untouched"


def test_memo_delete_keys_is_scoped_to_its_source(engine: Engine) -> None:
    """A delete under one source's scope does not touch an identical key under another source."""
    other_scope = "22222222-2222-2222-2222-222222222222"
    # The summary derivation key omits the source, so two sources can share a derivation_key;
    # scope_key is what keeps their memo entries distinct.
    memo_upsert_and_read(engine, _SUMMARY, _SCOPE, "shared-summary-key", "mine")
    memo_upsert_and_read(engine, _SUMMARY, other_scope, "shared-summary-key", "theirs")

    with begin_immediate(engine) as session:
        memo_delete_keys(session, _SCOPE, [(_SUMMARY, "shared-summary-key")])

    assert _read(engine, _SUMMARY, _SCOPE, "shared-summary-key") is None
    assert _read(engine, _SUMMARY, other_scope, "shared-summary-key") == "theirs"
