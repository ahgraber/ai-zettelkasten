# Expected Schema Changes: identity-provenance-foundation

## OpenAPI

The durable source identity is renamed `aizk_uuid` → `source_id` everywhere it appears in the public conversion API.
This is a **breaking change**, accepted pre-release with no external consumers.
No endpoints are added or removed and no structural shapes change — only the identity's name.

Concrete diff between `before/conversion-api-openapi.json` and the after-snapshot:

- **Path parameter rename.** `GET /v1/bookmarks/{aizk_uuid}/outputs` becomes
  `GET /v1/bookmarks/{source_id}/outputs`; the path-parameter `name` and the
  derived `operationId` update accordingly.
- **Query parameter rename.**
  The job-list `aizk_uuid` filter parameter becomes `source_id`.
- **Schema property rename.**
  The `aizk_uuid` property (and its entry in each schema's `required` array) becomes `source_id` in the affected request/response models.
- **Derived strings.**
  FastAPI-generated `title` values (`"Aizk Uuid"` → `"Source Id"`) and operation/response identifiers update as a consequence of the rename; they carry no contract meaning of their own.

`sdd-verify` SHOULD confirm the diff between
`.specs/changes/identity-provenance-foundation/schemas/before/conversion-api-openapi.json`
and the after-snapshot generated at verify time is exactly this `aizk_uuid` →
`source_id` rename, with no other additions, removals, or shape changes.

## Database

The renamed identity columns (`aizk_uuid` → `source_id`, `doc_id` → `source_id`, `scope_key` → `scope_id`) and the `graph_content_fts` recreate are applied by a single Alembic revision.
These tables are **not** tracked by the OpenAPI baseline; their structural fidelity is covered by the `schema-migrations` ORM-vs-migration equivalence test (`test_upgrade_produces_schema_matching_create_all`), not by `sdd-verify`.
