# Expected Schema Changes: deployment-trust-model

## OpenAPI

No changes expected.

Principal resolution, auth-mode startup validation, and trusted-host enforcement are server-side only.
`owner_id` is recorded internally on Source / Job / Output rows and is not surfaced in any request or response shape.
No new endpoints, no new request or response fields, no field renames or removals, no new error-response schemas.

`sdd-verify` SHOULD confirm an empty diff between
`.specs/changes/deployment-trust-model/schemas/before/conversion-api-openapi.json`
and the after-snapshot generated at verify time.

## Database

The change adds three new columns and three new indexes via a single Alembic migration.
These are NOT tracked by the OpenAPI schema baseline (the migration runner has its own equivalence test in `test_migrations.py` that compares the migrated schema against the SQLModel ORM baseline).
The shape of those database additions:

- `sources.owner_id` — `TEXT NOT NULL` (after backfill); index `ix_sources_owner_id` on `(owner_id)`.
- `conversion_jobs.owner_id` — `TEXT NOT NULL` (after backfill); index `ix_conversion_jobs_owner_id` on `(owner_id)`.
- `conversion_outputs.owner_id` — `TEXT NOT NULL` (after backfill); index `ix_conversion_outputs_owner_id` on `(owner_id)`.

Backfill value: `AIZK_DEFAULT_PRINCIPAL` (e.g., `"local"`) in `trust_network` mode at migration time.
Verification of the database additions is handled by the schema-migrations spec's existing ORM-baseline-equivalence requirement, not by `sdd-verify`.
