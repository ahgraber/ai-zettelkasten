"""Shared schema-fidelity comparison for migration-vs-``create_all`` parity tests.

Implements the equivalence definition in the ``schema-migrations`` spec: table
set; per-table column names, nullability, and SQLite type affinity; primary
keys; foreign keys; unique constraints; ``CHECK`` constraint expressions; and
index uniqueness plus partial-index predicates. CHECK expressions and partial
predicates are compared after whitespace and quoting normalization, since
SQLite's ``sqlite_master.sql`` text and SQLAlchemy's re-serialized predicate
text can otherwise differ only in cosmetic ways. Column default-value
expressions and comments are intentionally outside the comparison — SQLite and
Alembic normalize them differently, so comparing them would produce false
positives unrelated to schema drift.

Used by ``tests/db/migrations/test_graph_migrations.py``,
``tests/graph/test_mention_migrations.py``, and
``tests/db/migrations/test_conversion_migrations.py`` (the full cross-table
parity check). Importable from anywhere under ``tests/`` as
``tests.db.migrations._schema_equivalence`` (``--import-mode=importlib`` plus
the root ``tests/__init__.py`` makes ``tests`` a regular package).
"""

from __future__ import annotations

import re

from sqlalchemy.engine import Inspector

#: SQLite's column-affinity determination rules (datatype3.html #determination
#: of column affinity), applied in order: the first substring match wins. Used
#: to map a declared type name (e.g. ``VARCHAR(255)``, ``CHAR(32)``) to its
#: affinity class so ``VARCHAR`` vs ``TEXT`` is not a false positive while a
#: genuine ``INTEGER`` vs ``TEXT`` drift still fails.
_AFFINITY_RULES: tuple[tuple[str, str], ...] = (
    ("INT", "INTEGER"),
    ("CHAR", "TEXT"),
    ("CLOB", "TEXT"),
    ("TEXT", "TEXT"),
    ("BLOB", "BLOB"),
    ("REAL", "REAL"),
    ("FLOA", "REAL"),
    ("DOUB", "REAL"),
)


def _type_affinity(type_name: str) -> str:
    """Return the SQLite column affinity for a declared type name.

    Mirrors SQLite's own affinity-determination order; a declared type with no
    matching substring (and any empty type name) gets ``BLOB`` affinity,
    per SQLite's documented fallback.
    """
    upper = type_name.upper()
    for substring, affinity in _AFFINITY_RULES:
        if substring in upper:
            return affinity
    return "NUMERIC" if upper else "BLOB"


#: Matches a bareword identifier quoted SQLite's three identifier-quoting ways
#: (``"col"``, `` `col` ``, ``[col]``). Deliberately does not match single-quoted
#: text (SQL string literals, e.g. ``'active'``), so a quoted string value inside
#: an expression is never mistaken for a quoted identifier.
_QUOTED_IDENTIFIER = re.compile(r'"([A-Za-z_][A-Za-z0-9_]*)"|`([A-Za-z_][A-Za-z0-9_]*)`|\[([A-Za-z_][A-Za-z0-9_]*)\]')


def _normalize_expression(expr: str) -> str:
    """Normalize a CHECK expression or partial-index predicate for comparison.

    Collapses whitespace runs to a single space, strips leading/trailing
    whitespace, and unquotes identifiers wrapped in any of SQLite's three
    identifier-quoting styles (``"col"``, `` `col` ``, ``[col]``) to their
    bareword form — Alembic and raw ``sqlite_master.sql`` text do not
    consistently agree on whether a column reference is quoted. Single-quoted
    string literals (e.g. ``'active'``) are left untouched, since unquoting them
    would change the expression's meaning rather than normalize cosmetic
    variance.
    """
    collapsed = re.sub(r"\s+", " ", expr).strip()
    return _QUOTED_IDENTIFIER.sub(lambda m: next(g for g in m.groups() if g is not None), collapsed)


def _normalize_index(idx: dict) -> tuple:
    """Return a comparable key for an index: name, columns, uniqueness, predicate."""
    where = idx.get("dialect_options", {}).get("sqlite_where")
    predicate = _normalize_expression(str(where)) if where is not None else None
    return (idx["name"], tuple(sorted(idx["column_names"])), bool(idx.get("unique", False)), predicate)


def _normalize_fk(fk: dict) -> tuple:
    """Return a comparable key for a foreign key: columns, referred table/columns."""
    return (
        tuple(sorted(fk["constrained_columns"])),
        fk["referred_table"],
        tuple(sorted(fk["referred_columns"])),
    )


def _normalize_check(ck: dict) -> tuple:
    """Return a comparable key for a CHECK constraint: name and normalized expression."""
    return (ck["name"], _normalize_expression(ck["sqltext"]))


def assert_table_schema_equivalent(migrated: Inspector, baseline: Inspector, table: str) -> None:
    """Assert one table's schema is equivalent between a migrated and baseline inspector.

    Compares column names, nullability, and type affinity; primary keys;
    foreign keys; unique constraints; CHECK constraint expressions; and indexes
    (uniqueness and partial predicate), per the ``schema-migrations`` spec's
    extended equivalence definition. Raises ``AssertionError`` with a
    table-scoped message on the first mismatched dimension.
    """
    baseline_cols = {c["name"]: (c["nullable"], _type_affinity(str(c["type"]))) for c in baseline.get_columns(table)}
    migrated_cols = {c["name"]: (c["nullable"], _type_affinity(str(c["type"]))) for c in migrated.get_columns(table)}
    assert migrated_cols == baseline_cols, f"{table} column/nullable/type-affinity mismatch"

    assert {_normalize_index(i) for i in migrated.get_indexes(table)} == {
        _normalize_index(i) for i in baseline.get_indexes(table)
    }, f"{table} index mismatch"

    assert {_normalize_fk(fk) for fk in migrated.get_foreign_keys(table)} == {
        _normalize_fk(fk) for fk in baseline.get_foreign_keys(table)
    }, f"{table} foreign key mismatch"

    baseline_pk = sorted(baseline.get_pk_constraint(table)["constrained_columns"])
    migrated_pk = sorted(migrated.get_pk_constraint(table)["constrained_columns"])
    assert migrated_pk == baseline_pk, f"{table} primary key mismatch"

    baseline_uniques = {tuple(sorted(uc["column_names"])) for uc in baseline.get_unique_constraints(table)}
    migrated_uniques = {tuple(sorted(uc["column_names"])) for uc in migrated.get_unique_constraints(table)}
    assert migrated_uniques == baseline_uniques, f"{table} unique constraint mismatch"

    baseline_checks = {_normalize_check(ck) for ck in baseline.get_check_constraints(table)}
    migrated_checks = {_normalize_check(ck) for ck in migrated.get_check_constraints(table)}
    assert migrated_checks == baseline_checks, f"{table} CHECK constraint mismatch"
