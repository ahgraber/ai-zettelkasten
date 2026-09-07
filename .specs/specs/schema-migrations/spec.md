# Schema Migrations Specification

> Synced from change `output-sensitive-identity` on 2026-09-06

## Purpose

Evolve the shared SQLite schema over time using Alembic migrations while guaranteeing that the applied schema stays faithful to the ORM models and that migrations remain fully reversible.
The chain starts from one baseline revision established at the `0.2.0` database rebuild; a database stamped before that boundary is refused rather than migrated.
The migration runner is invoked during API lifespan and by tests, so it MUST work without requiring an `alembic.ini` on disk or a specific working directory.

## Requirements

### Requirement: Applied migrations produce a schema equivalent to the ORM model baseline

After upgrading to head, the database's observable schema SHALL be structurally equivalent to the schema that `SQLModel.metadata.create_all()` would produce from the current ORM models, modulo the `alembic_version` tracking table.
Equivalence covers the set of tables; the set, nullability, and type affinity of columns per table; the set of indexes, foreign keys, and unique constraints; each CHECK constraint's expression; and each index's uniqueness and partial-index predicate — expressions and predicates compared after whitespace and quoting normalization.
Column default-value expressions and comments are outside the equivalence contract because SQLite and Alembic normalize them differently.
This guarantee is what prevents production migrations from drifting away from the code that reads and writes the database — including the constraint classes that enforce data invariants directly: CHECK-guarded discriminators and conditions, and partial unique indexes whose predicates scope uniqueness.

#### Scenario: Table set matches the ORM baseline

- **GIVEN** a freshly migrated database and a baseline database produced by `SQLModel.metadata.create_all()`
- **WHEN** both are inspected
- **THEN** the set of user tables (excluding `alembic_version`) is identical across the two databases

#### Scenario: Per-table column shape matches the ORM baseline

- **GIVEN** a table present in both the migrated and baseline databases
- **WHEN** columns are compared
- **THEN** the column name sets are identical and each column's nullability and type affinity match between migrated and baseline

#### Scenario: Indexes, foreign keys, and unique constraints match the ORM baseline

- **GIVEN** a table present in both the migrated and baseline databases
- **WHEN** indexes, foreign keys, and unique constraints are compared
- **THEN** each set (normalized by name and column membership) is identical between migrated and baseline

#### Scenario: CHECK expressions and partial-index predicates match the ORM baseline

- **GIVEN** a table carrying CHECK constraints or partial unique indexes in both the migrated and baseline databases
- **WHEN** the constraint expressions and index predicates are compared after normalization
- **THEN** each CHECK constraint's expression and each index's partial predicate is identical between migrated and baseline, and each index's uniqueness matches

---

### Requirement: Migrations are reversible end-to-end

The migration chain SHALL support a full upgrade-to-head followed by downgrade-to-base round-trip on databases whose data content is representable in the pre-migration schema, without leaving residual user tables.
For any future migration that widens a table's data model (admitting rows that have no pre-migration equivalent), the `downgrade()` SHALL be conditional: it SHALL detect such rows up front and abort with a clear operator-facing error before performing any destructive change.
CI SHALL continue to validate that every migration's `downgrade()` is implemented.

#### Scenario: Full round-trip on an empty database leaves no user tables

- **GIVEN** a fresh database with no user rows
- **WHEN** all migrations are applied with `upgrade(head)` and then reversed with `downgrade(base)`
- **THEN** the only remaining table is `alembic_version` (or the database is otherwise empty of user tables)

---

### Requirement: The migration history is a single baseline at the rebuild boundary

The migration chain SHALL consist of a single baseline revision producing the current ORM-equivalent schema, established at the database rebuild that accompanies the `0.2.0` version.
A database stamped with any pre-rebuild revision SHALL NOT be upgradeable: the runner SHALL refuse it with an error that names the rebuild path, and SHALL change nothing.

#### Scenario: A fresh database migrates to head in one revision

- **GIVEN** an empty database
- **WHEN** migrations are applied to head
- **THEN** exactly one revision applies, and the schema is ORM-baseline equivalent

#### Scenario: A legacy-stamped database is refused

- **GIVEN** a database whose `alembic_version` names a pre-rebuild revision
- **WHEN** an upgrade is attempted
- **THEN** the runner refuses with an error naming the rebuild path, and the database is unchanged

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

- **Implementation:** `src/aizk/db/migrations/__init__.py` (`run_migrations`, the legacy-stamp refusal), `src/aizk/db/migrations/env.py` (Alembic environment), `src/aizk/db/migrations/versions/*.py` (migration scripts)
- **Tests:** `tests/db/migrations/`
- **Dependencies:** `alembic`, `sqlmodel`, `sqlalchemy.inspect`, the datamodel modules imported for `SQLModel.metadata` registration
- **Invocation Sites:** FastAPI lifespan (`aizk.conversion.api.main.lifespan`), worker startup, test fixtures (`db_engine`), and direct script usage
- **Equivalence Metric:** Structural, not textual.
  Column default-value expressions and comments are explicitly outside the equivalence contract because SQLite and Alembic normalize them differently; the equivalence check covers shape (names, nullability, indexes, keys, constraints).
- **Scope Boundary:** This spec covers the migration runner contract and the migration-chain integrity invariants.
  Individual migration scripts' forward semantics (what a given column means) are covered by the owning datamodel spec, not here.
