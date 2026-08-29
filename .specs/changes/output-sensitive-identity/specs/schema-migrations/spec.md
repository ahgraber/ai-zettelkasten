# Delta for schema-migrations

## ADDED Requirements

### Requirement: The migration history is a single baseline at the rebuild boundary

The migration chain SHALL consist of a single baseline revision producing the current ORM-equivalent schema, established at the database rebuild that accompanies the `0.2.0` version.
A database stamped with any pre-rebuild revision SHALL NOT be upgradeable: the runner SHALL refuse it with an error that names the rebuild path, and SHALL change nothing.

Serves: rebuildable-corpus

#### Scenario: A fresh database migrates to head in one revision

- **GIVEN** an empty database
- **WHEN** migrations are applied to head
- **THEN** exactly one revision applies, and the schema is ORM-baseline equivalent

#### Scenario: A legacy-stamped database is refused

- **GIVEN** a database whose `alembic_version` names a pre-rebuild revision
- **WHEN** an upgrade is attempted
- **THEN** the runner refuses with an error naming the rebuild path, and the database is unchanged

## MODIFIED Requirements

### Requirement: Migrations are reversible end-to-end

> Previously: the requirement carried the conditional-downgrade contract of the pre-rebuild chain, including the `bookmarks → sources` migration's KaraKeep-specific downgrade conditions; that chain is removed by the re-baseline.

The migration chain SHALL support a full upgrade-to-head followed by downgrade-to-base round-trip on databases whose data content is representable in the pre-migration schema, without leaving residual user tables.
For any future migration that widens a table's data model (admitting rows that have no pre-migration equivalent), the `downgrade()` SHALL be conditional: it SHALL detect such rows up front and abort with a clear operator-facing error before performing any destructive change.
CI SHALL continue to validate that every migration's `downgrade()` is implemented.

Serves: rebuildable-corpus

<!-- modified-removes: Downgrade round-trip succeeds when only KaraKeep-backed sources exist, Downgrade aborts when non-KaraKeep sources exist -->

#### Scenario: Full round-trip on an empty database leaves no user tables

- **GIVEN** a fresh database with no user rows
- **WHEN** all migrations are applied with `upgrade(head)` and then reversed with `downgrade(base)`
- **THEN** the only remaining table is `alembic_version` (or the database is otherwise empty of user tables)

## REMOVED Requirements

### Requirement: Enforce source_ref and source_ref_hash as NOT NULL in the sources table

Removed because: the migration script it specifies is removed by the re-baseline.
The invariant survives at its owning layers: `conversion-api` requires every Source to be materialized at submit time with `source_ref` and `source_ref_hash` populated and immutable, and the NOT NULL shape lives in the ORM models under the ORM-baseline equivalence requirement.

### Requirement: Add owner_id columns to sources, conversion_jobs, and conversion_outputs

Removed because: the migration script and its backfill are removed by the re-baseline.
The invariant survives at its owning layers: `conversion-api` requires `owner_id = principal.subject` persisted on every Source and Job row at creation (and prohibits it from request/response schemas), and the columns, nullability, and indexes live in the ORM models under the ORM-baseline equivalence requirement.
The rebuild re-ingests with `owner_id` populated at insert time, so no backfill path exists to specify.

### Requirement: Enforce owner-scoped uniqueness for job idempotency keys

Removed because: the migration script it specifies is removed by the re-baseline.
The invariant survives at its owning layers: `conversion-api` requires owner-scoped job deduplication (duplicate detection matches only when both `idempotency_key` and `owner_id` match, and a shared Source row never causes cross-principal Job reuse), and the composite `(owner_id, idempotency_key)` uniqueness lives in the ORM models under the ORM-baseline equivalence requirement.
