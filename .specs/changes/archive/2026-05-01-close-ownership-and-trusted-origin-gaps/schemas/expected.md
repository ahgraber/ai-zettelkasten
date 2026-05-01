# Expected Schema Changes: close-ownership-and-trusted-origin-gaps

## OpenAPI

No changes expected.

This change tightens authorization and outbound-trust semantics only.
No new endpoints, no new request or response fields, no field renames or removals,
and no new OpenAPI error schemas are expected.

`sdd-verify` SHOULD confirm an empty diff between
`.specs/changes/close-ownership-and-trusted-origin-gaps/schemas/before/conversion-api-openapi.json`
and the after-snapshot generated at verify time.

## Database

The output-ownership and KaraKeep-origin parts of this change do not alter the
database shape.

The idempotency part is expected to change the `conversion_jobs` uniqueness model:

- remove the global unique/index shape on `idempotency_key`
- replace it with an owner-scoped unique/index shape on `(owner_id, idempotency_key)`

This database change is not represented in the OpenAPI snapshot.
Verification belongs in the migration equivalence tests and the
`schema-migrations` delta for this change.
