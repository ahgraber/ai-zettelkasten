# Delta for schema-migrations

## MODIFIED Requirements

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

## Technical Notes (delta)

- **Rationale:** The widening migration is non-destructive on the forward path but inherently one-way for data that has no pre-migration representation.
  Aborting on data loss is strictly safer than a silent lossy downgrade and preserves the operator-facing value of `downgrade()` for the common rollback case (rolling back before any non-KaraKeep traffic has been accepted).
- **Scope:** This delta modifies only the reversibility contract.
  The other schema-migrations requirements (ORM-baseline equivalence, self-contained runner) are unchanged.
- **Interaction with cutover:** Because `IngressPolicy.accepted_submission_kinds = {"karakeep_bookmark"}` at cutover, freshly-cut deployments accept no non-KaraKeep ingress and therefore remain downgrade-safe until the policy is widened.
  The conditional abort exists to protect deployments that have already widened `IngressPolicy` and begun ingesting non-KaraKeep sources.
