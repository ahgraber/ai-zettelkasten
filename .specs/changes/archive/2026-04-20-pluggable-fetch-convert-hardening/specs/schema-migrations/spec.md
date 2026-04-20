# Delta for schema-migrations

## ADDED Requirements

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
