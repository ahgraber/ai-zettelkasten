# Delta for schema-migrations

## ADDED Requirements

### Requirement: Add owner_id columns to sources, conversion_jobs, and conversion_outputs

The system SHALL add an Alembic migration that introduces an `owner_id` column on each of `sources`, `conversion_jobs`, and `conversion_outputs`, backfills existing rows with the value of the deployment's `AIZK_DEFAULT_PRINCIPAL` setting at migration time, and finalizes each column as `NOT NULL`.
The migration SHALL also add a single-column index on `owner_id` for each of the three tables.

The migration SHALL execute the steps in the following order so that the database is never in a state where a NOT NULL `owner_id` column has unbackfilled rows:

1. Add each `owner_id` column as `NULLABLE` with no server-side default.
2. Backfill every existing row with the value resolved from `AIZK_DEFAULT_PRINCIPAL`; the migration SHALL read this value once at migration start and apply it uniformly across all three tables.
3. Verify that no row in any of the three tables has `owner_id IS NULL`; if any NULLs remain after the backfill, raise `IrreversibleMigrationError` and abort before altering nullability.
4. `ALTER COLUMN ... NOT NULL` for each `owner_id` column.
5. Create the three single-column indexes.

The backfill value persists in the database after migration; subsequent rows written by application code carry whatever `owner_id` the API or worker resolves at insert time, which at cutover is also `AIZK_DEFAULT_PRINCIPAL`.

The migration's `downgrade()` SHALL drop the three indexes, then drop the three columns.
Reversibility is unconditional — dropping columns does not require row-level inspection because every row carries a single backfilled value at upgrade time and the column is internal (not exposed in OpenAPI).

This requirement extends the precedent established by the `source_ref` / `source_ref_hash` NOT NULL migration: a NOT NULL column transition that is gated by an explicit pre-alter assertion to fail loud on a partially-backfilled database rather than silently truncating data.

#### Scenario: Migration adds and backfills owner_id columns

- **GIVEN** a database with rows in `sources`, `conversion_jobs`, and `conversion_outputs` that pre-date this migration, and `AIZK_DEFAULT_PRINCIPAL=local` set at migration time
- **WHEN** the upgrade migration runs
- **THEN** every existing row in all three tables has `owner_id = "local"`, all three `owner_id` columns are `NOT NULL`, and the three indexes are present

#### Scenario: Migration aborts when backfill leaves NULL rows

- **GIVEN** a contrived scenario where the backfill UPDATE statement fails to populate every row (e.g., a concurrent write inserted a NULL during the window)
- **WHEN** the pre-alter NULL assertion runs
- **THEN** `IrreversibleMigrationError` is raised before any `ALTER COLUMN ... NOT NULL` is executed, the error message identifies the offending row count per table, and the schema retains the NULLABLE shape

#### Scenario: Downgrade drops owner_id columns and indexes unconditionally

- **GIVEN** a database upgraded to the post-migration state with every row carrying a non-null `owner_id`
- **WHEN** the downgrade migration runs
- **THEN** the three indexes are dropped, the three `owner_id` columns are dropped, and all other row data is preserved

#### Scenario: ORM-baseline equivalence holds after migration

- **GIVEN** a freshly migrated database (upgrade to head including this migration)
- **WHEN** its schema is compared to `SQLModel.metadata.create_all()` output
- **THEN** the presence, type (`TEXT`), nullability (`NOT NULL`), and the single-column indexes for `owner_id` on all three tables are identical between the migrated and baseline databases
