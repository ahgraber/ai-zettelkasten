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

The migration chain SHALL support a full upgrade-to-head followed by downgrade-to-base round-trip on databases whose data content is representable in the pre-migration schema, without leaving residual user tables.
For migrations that widen a table's data model (e.g., admitting rows that have no pre-migration equivalent), the `downgrade()` SHALL be conditional: it SHALL detect such rows up front and abort with a clear operator-facing error before performing any destructive change.
Specifically, the `bookmarks → sources` migration introduced by this change admits Source rows whose `karakeep_id` is null (non-KaraKeep-backed sources).
Its `downgrade()` SHALL abort with an `IrreversibleMigrationError` when any `sources` row has `karakeep_id IS NULL`, and SHALL otherwise complete the reverse transformation (restore `karakeep_id` NOT NULL, drop `source_ref` and `source_ref_hash`, rename back to `bookmarks`).
This conditional-reversibility contract replaces the prior unconditional guarantee: operators CAN still roll back when no non-KaraKeep data has been ingested, and cannot silently destroy non-KaraKeep Source rows when it has.
CI SHALL continue to validate that every migration's `downgrade()` is implemented; round-trip tests SHALL cover both the empty-database path and the populated-non-KaraKeep-row path.

#### Scenario: Full round-trip on an empty database leaves no user tables

- **GIVEN** a fresh database with no user rows
- **WHEN** all migrations are applied with `upgrade(head)` and then reversed with `downgrade(base)`
- **THEN** the only remaining table is `alembic_version` (or the database is otherwise empty of user tables)

#### Scenario: Downgrade round-trip succeeds when only KaraKeep-backed sources exist

- **GIVEN** a migrated database containing only `sources` rows with non-null `karakeep_id`
- **WHEN** the `bookmarks → sources` migration is reversed
- **THEN** the downgrade completes, the table is renamed back to `bookmarks`, `karakeep_id` is NOT NULL, and `source_ref` / `source_ref_hash` columns are dropped

#### Scenario: Downgrade aborts when non-KaraKeep sources exist

- **GIVEN** a migrated database containing at least one `sources` row with `karakeep_id IS NULL`
- **WHEN** the operator invokes `downgrade()` across the `bookmarks → sources` migration
- **THEN** the migration raises `IrreversibleMigrationError` before altering schema, the error message identifies the offending row count, and the schema remains at the post-upgrade shape

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

### Requirement: Enforce source_ref and source_ref_hash as NOT NULL in the sources table

The system SHALL add an Alembic migration that alters `sources.source_ref` and `sources.source_ref_hash` to NOT NULL.
Before altering schema, the migration SHALL assert that no row has a NULL value in either column and SHALL raise `IrreversibleMigrationError` if any NULL is found, so the operation aborts loudly on a partially-backfilled database rather than silently truncating data.
After this migration the database schema SHALL match the SQLModel definition, which removes `nullable=True` from both fields; the ORM-baseline equivalence test in `test_migrations.py` provides the automated safety net.
The migration's `downgrade()` reverses the NOT NULL constraint by rebuilding the table with both columns nullable, restoring the pre-upgrade shape.

(Context: the `pluggable-fetch-convert` cutover migration left both columns nullable because the backfill only targeted rows with non-null `karakeep_id`; any legacy row without a `karakeep_id` would have failed the backfill.
Every row written by the post-cutover API has both columns populated.
This migration closes the schema-vs-invariant gap documented in the `pluggable-fetch-convert` design.)

#### Scenario: Migration aborts when NULL source_ref rows exist

- **GIVEN** a database where at least one `sources` row has `source_ref IS NULL`
- **WHEN** the upgrade migration runs
- **THEN** `IrreversibleMigrationError` is raised before any schema change, the error message
  identifies the count of offending rows, and the schema remains at the pre-upgrade shape

#### Scenario: Migration succeeds on a fully-backfilled database

- **GIVEN** a database where every `sources` row has non-null `source_ref` and `source_ref_hash`
- **WHEN** the upgrade migration runs
- **THEN** both columns become NOT NULL, the unique index on `source_ref_hash` is preserved,
  and all existing rows are intact

#### Scenario: Downgrade restores nullable columns

- **GIVEN** a database upgraded to the NOT NULL state
- **WHEN** the downgrade migration runs
- **THEN** both `source_ref` and `source_ref_hash` revert to nullable TEXT, and all row data
  is preserved

#### Scenario: ORM-baseline equivalence holds after migration

- **GIVEN** a freshly migrated database (upgrade to head including this migration)
- **WHEN** its schema is compared to `SQLModel.metadata.create_all()` output
- **THEN** column nullability for `source_ref` and `source_ref_hash` is identical between
  the migrated and baseline databases

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
