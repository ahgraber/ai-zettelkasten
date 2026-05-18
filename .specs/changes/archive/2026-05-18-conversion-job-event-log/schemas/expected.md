# Expected Schema Diff

## OpenAPI (`conversion-api-openapi.json`)

**No diff expected.**

This change adds a new database table and an internal `record_transition` helper.
It does not add, modify, or remove any API endpoint, request schema, or response schema.
The `before` snapshot is the expected `after` snapshot — `sdd-verify` should observe zero structural diff between `schemas/before/conversion-api-openapi.json` and any post-implementation regeneration.

## Database (out-of-band, not OpenAPI-tracked)

A new SQLite table `conversion_job_events` is added via Alembic migration.
This is not reflected in the OpenAPI snapshot and is verified through:

- The Alembic migration file under `src/aizk/conversion/migrations/versions/`.
- Migration tests in `tests/conversion/integration/test_migrations.py`.
- SQLModel table definition in `src/aizk/conversion/datamodel/`.
