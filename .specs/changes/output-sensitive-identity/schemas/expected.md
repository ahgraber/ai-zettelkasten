# Expected Schema Diff: output-sensitive-identity

## conversion-api-openapi

No changes expected.
The bulk-action ceiling was already published from `aizk.pipeline.lifecycle` before this change; `maxItems` stays `100`.

## graph-api-openapi

No changes expected.
Intake keeps its documented response codes (`201` created, `200` reused, `404` unknown reference, `503` at capacity).
The `(job, created)` return-signature change and the creation event are internal: they alter how the routes decide the status code and what is logged, not the API contract.

## Verify-time rule

A non-empty diff against either `before/` snapshot is unplanned.
If one appears, explain it in `design.md` and update this file before sync, or revert the API-visible drift.

Database schema changes (new provenance/applicability tables, `Uuid` retypes, opaque `derivation_key` values, squashed Alembic baseline) are covered by the `schema-migrations` ORM-equivalence fixtures, not by these OpenAPI snapshots.
