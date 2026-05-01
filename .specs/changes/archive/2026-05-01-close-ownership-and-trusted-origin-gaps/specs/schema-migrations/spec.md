# Delta for schema-migrations — close-ownership-and-trusted-origin-gaps

## ADDED Requirements

### Requirement: Enforce owner-scoped uniqueness for job idempotency keys

The system SHALL evolve the `conversion_jobs` uniqueness model from global
`idempotency_key` uniqueness to owner-scoped uniqueness on
`(owner_id, idempotency_key)`.
After this migration, two different owners MAY hold rows with the same
`idempotency_key`, while a single owner still cannot hold duplicate rows for the same
key.

The upgrade migration SHALL:

1. drop the legacy single-column unique/index shape on `idempotency_key`
2. create a composite unique/index shape on `(owner_id, idempotency_key)`
3. preserve all existing rows without rewriting `idempotency_key` values

Because all pre-migration rows already carry `owner_id`, no data backfill is needed
for this change.
In the current single-principal `trust_network` deployment, the new composite shape
is equivalent to the old behavior for existing data while permitting future
multi-principal duplicates safely.

The migration's `downgrade()` SHALL be conditional.
Before restoring global uniqueness on `idempotency_key`, it SHALL check whether any
two rows with different `owner_id` values share the same `idempotency_key`.
If such rows exist, `downgrade()` SHALL raise `IrreversibleMigrationError` before any
schema change, because collapsing back to global uniqueness would destroy valid
post-change data.

#### Scenario: Upgrade permits same key across different owners

- **GIVEN** a database upgraded to the new schema
- **WHEN** two rows are inserted into `conversion_jobs` with the same
  `idempotency_key` but different `owner_id` values
- **THEN** both inserts succeed

#### Scenario: Upgrade still rejects duplicate key for the same owner

- **GIVEN** a database upgraded to the new schema
- **WHEN** two rows are inserted into `conversion_jobs` with the same
  `owner_id` and the same `idempotency_key`
- **THEN** the second insert is rejected by the composite uniqueness constraint

#### Scenario: Downgrade aborts when cross-owner duplicates exist

- **GIVEN** a migrated database containing at least two rows with the same
  `idempotency_key` and different `owner_id` values
- **WHEN** the operator invokes `downgrade()` across this migration
- **THEN** `IrreversibleMigrationError` is raised before any schema change, the error
  message identifies the conflicting row count, and the schema remains at the
  post-upgrade shape

#### Scenario: ORM-baseline equivalence holds after migration

- **GIVEN** a freshly migrated database (upgrade to head including this migration)
- **WHEN** its schema is compared to `SQLModel.metadata.create_all()` output
- **THEN** the `conversion_jobs` unique/index shape matches the ORM baseline: no
  global unique index on `idempotency_key`, and a composite unique/index on
  `(owner_id, idempotency_key)` is present
