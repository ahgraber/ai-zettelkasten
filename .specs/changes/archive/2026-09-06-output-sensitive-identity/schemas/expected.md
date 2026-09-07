# Expected Schema Diff: output-sensitive-identity

## conversion-api-openapi

No changes expected.
The bulk-action ceiling was already published from `aizk.pipeline.lifecycle` before this change; `maxItems` stays `100`.

## graph-api-openapi

No contract changes expected.
Intake keeps its documented response codes (`201` created, `200` reused, `404` unknown reference, `503` at capacity).
The `(job, created)` return-signature change and the creation event are internal: they alter how the routes decide the status code and what is logged, not the API contract.

Six route `description` strings differ from the `before/` snapshot.
FastAPI publishes each route's docstring as the operation description, and the docstring pass over `src/aizk/graph` rewrote them.
Paths, operations, parameters, response models, and status codes are identical.

## Verify-time rule

A diff that changes a path, operation, parameter, response model, or status code against either `before/` snapshot is unplanned.
If one appears, explain it in `design.md` and update this file before sync, or revert the API-visible drift.
A `description`-only diff is documentation prose; record it here with its cause.

Database schema changes (new provenance/applicability tables, `Uuid` retypes, opaque `derivation_key` values, squashed Alembic baseline) are covered by the `schema-migrations` ORM-equivalence fixtures, not by these OpenAPI snapshots.
