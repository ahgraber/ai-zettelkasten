# Delta for schema-migrations

## MODIFIED Requirements

### Requirement: Applied migrations produce a schema equivalent to the ORM model baseline

After upgrading to head, the database's observable schema SHALL be structurally equivalent to the schema that `SQLModel.metadata.create_all()` would produce from the current ORM models, modulo the `alembic_version` tracking table.
Equivalence covers the set of tables; the set, nullability, and type affinity of columns per table; the set of indexes, foreign keys, and unique constraints; each CHECK constraint's expression; and each index's uniqueness and partial-index predicate — expressions and predicates compared after whitespace and quoting normalization.
Column default-value expressions and comments are outside the equivalence contract because SQLite and Alembic normalize them differently.
This guarantee is what prevents production migrations from drifting away from the code that reads and writes the database — including the constraint classes that enforce data invariants directly: CHECK-guarded discriminators and conditions, and partial unique indexes whose predicates scope uniqueness.

Serves: replayable-duplicate-free-dataset

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
