# Schema Migrations Specification

> Generated from code analysis on 2026-04-15
> Source files: `src/aizk/conversion/migrations/__init__.py`, `src/aizk/conversion/migrations/env.py`, `src/aizk/conversion/migrations/versions/`, `tests/conversion/unit/test_migrations.py`

## Purpose

Evolve the conversion service's SQLite schema over time using Alembic migrations while guaranteeing that the applied schema stays faithful to the ORM models and that migrations remain fully reversible.
The migration runner is invoked during API lifespan and by tests, so it MUST work without requiring an `alembic.ini` on disk or a specific working directory.

## Requirements

### Requirement: Applied migrations produce a schema equivalent to the ORM model baseline

After upgrading to head, the database's observable schema SHALL be structurally equivalent to the schema that `SQLModel.metadata.create_all()` would produce from the current ORM models, modulo the `alembic_version` tracking table.
Equivalence covers the set of tables, the set and nullability of columns per table, and the set of indexes, foreign keys, and unique constraints.
This guarantee is what prevents production migrations from drifting away from the code that reads and writes the database.

#### Scenario: Table set matches the ORM baseline

- **GIVEN** a freshly migrated database and a baseline database produced by `SQLModel.metadata.create_all()`
- **WHEN** both are inspected
- **THEN** the set of user tables (excluding `alembic_version`) is identical across the two databases

#### Scenario: Per-table column shape matches the ORM baseline

- **GIVEN** a table present in both the migrated and baseline databases
- **WHEN** columns are compared
- **THEN** the column name sets are identical and each column's nullability matches between migrated and baseline

#### Scenario: Indexes, foreign keys, and unique constraints match the ORM baseline

- **GIVEN** a table present in both the migrated and baseline databases
- **WHEN** indexes, foreign keys, and unique constraints are compared
- **THEN** each set (normalized by name and column membership) is identical between migrated and baseline

---

### Requirement: Migrations are reversible end-to-end

The migration chain SHALL support a full upgrade-to-head followed by downgrade-to-base round-trip without leaving residual user tables.
This guarantee lets operators roll back migrations during incidents and lets CI validate that every migration's `downgrade()` is actually implemented.

#### Scenario: Full round-trip leaves no user tables

- **GIVEN** a fresh database
- **WHEN** all migrations are applied with `upgrade(head)` and then reversed with `downgrade(base)`
- **THEN** the only remaining table is `alembic_version` (or the database is otherwise empty of user tables)

---

### Requirement: Migration runner is self-contained

The system SHALL expose a programmatic migration runner that accepts an optional database URL override and does not require an `alembic.ini` file on disk or any particular working directory.
This lets the API lifespan, test fixtures, and ad-hoc scripts all drive migrations the same way against distinct database targets.

#### Scenario: Runner accepts a database URL override

- **GIVEN** a database URL passed explicitly to the runner
- **WHEN** migrations are run
- **THEN** migrations execute against that URL regardless of the ambient configuration

#### Scenario: Runner requires no on-disk alembic config

- **GIVEN** a process with no `alembic.ini` on disk and an arbitrary current working directory
- **WHEN** the runner is invoked
- **THEN** migrations execute successfully using the script location resolved from the migrations package

---

## Technical Notes

- **Implementation:** `src/aizk/conversion/migrations/__init__.py` (`run_migrations`), `src/aizk/conversion/migrations/env.py` (Alembic environment), `src/aizk/conversion/migrations/versions/*.py` (migration scripts)
- **Tests:** `tests/conversion/unit/test_migrations.py`
- **Dependencies:** `alembic`, `sqlmodel`, `sqlalchemy.inspect`, `aizk.conversion.datamodel` (imported for `SQLModel.metadata` registration)
- **Invocation Sites:** FastAPI lifespan (`aizk.conversion.api.main.lifespan`), worker startup, test fixtures (`db_engine`), and direct script usage
- **Equivalence Metric:** Structural, not textual.
  Column default-value expressions and comments are explicitly outside the equivalence contract because SQLite and Alembic normalize them differently; the equivalence check covers shape (names, nullability, indexes, keys, constraints).
- **Scope Boundary:** This spec covers the migration runner contract and the migration-chain integrity invariants.
  Individual migration scripts' forward semantics (what a given column means) are covered by the owning datamodel spec, not here.
