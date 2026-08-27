# Expected Schema Changes: Pipeline Work Admission

## OpenAPI — graph-api-openapi

### Added

- `POST /v1/contextualizations`: submits contextualization work for a conversion output.
  Responds `201` with the created work-unit, `200` with the existing work-unit when the submission resolves to work already enqueued, `404` when the referenced conversion output does not exist, and `503` with a `Retry-After` header when the stage is at its declared capacity.
- `POST /v1/extractions`: submits extraction work for a source.
  Responds `201` with the created work-unit, `200` with the existing work-unit when the submission resolves to work already enqueued, `404` when the referenced source does not exist, and `503` with a `Retry-After` header when the stage is at its declared capacity.
- Request schema for the contextualization submission, carrying the conversion-output reference, bounded to the persistent key range.
- Request schema for the extraction submission, carrying the source identity.
- Queue-full response schema, mirroring the conversion API's `QueueFullResponse` shape (`detail` plus `retry_after`).

Each submission declares all five responses it can return — `200`, `201`, `404`, `422`, `503` — and the `503` declares the `Retry-After` header, so a generated client is not surprised at runtime.

### Modified

- `/v1/contextualizations` and `/v1/extractions` each gain a `post` operation alongside their existing `get`.
  The existing `get` operations, the per-unit `get`, `retry`, and `cancel` operations, and the `ContextualizationJobResponse` / `ExtractionJobResponse` / `WorkUnitStatus` schemas are unchanged.

## OpenAPI — conversion-api-openapi

No change.
This change adds no conversion API operation and modifies no conversion request or response model.
