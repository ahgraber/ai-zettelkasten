# Proposal: close-ownership-and-trusted-origin-gaps

## Intent

The branch already made job visibility owner-scoped and introduced `owner_id` on
Sources, Jobs, and Outputs, but the security review found three contract gaps that
still matter before merge:

1. Output read surfaces are not specified as owner-scoped, so a caller who knows a
   bookmark `aizk_uuid` or `output_id` can still retrieve another owner's output
   records or stored artifacts.
2. Duplicate-submission idempotency is still specified as global on
   `idempotency_key`, which conflicts with the existing ownership model: later
   principals are supposed to be able to submit the same source/config and create
   their own Job without seeing the first principal's row.
3. The KaraKeep trusted-infrastructure carve-out is specified only loosely.
   Without an exact-origin rule, an implementation can treat a lookalike prefix host
   as operator-trusted and bypass the egress gate.

This change closes those gaps by tightening the API ownership contract and making
the KaraKeep trust boundary exact instead of prefix-based.

## Scope

**In scope:**

- Owner-scope `GET /v1/bookmarks/{aizk_uuid}/outputs` by `ConversionOutput.owner_id`
- Owner-check `GET /v1/outputs/{output_id}/manifest`,
  `GET /v1/outputs/{output_id}/markdown`, and
  `GET /v1/outputs/{output_id}/figures/{filename}` with a 404 posture on
  cross-owner access
- Clarify that duplicate-submission detection is scoped by
  `(principal.subject, idempotency_key)`, not by `idempotency_key` alone
- Capture the likely supporting schema change: owner-scoped uniqueness for job
  idempotency keys
- Tighten the KaraKeep carve-out so only the exact configured KaraKeep origin and
  operator-owned asset path prefix bypass the egress helper

**Out of scope:**

- New auth modes, roles, or admin/global-read exceptions
- Changing API request or response schemas
- Retry-classification fixes such as `FetchTooLargeError` propagation
- Dependency churn, README cleanups, or unrelated docs-only follow-ups

## Approach

1. **Output ownership follows the Output row, not the Source row.**
   Source rows are intentionally shared by `source_ref_hash`, so bookmark-output
   listing must filter by `ConversionOutput.owner_id == principal.subject` rather
   than by `Source.owner_id`.

2. **Idempotency is owner-scoped without changing the hash formula.**
   The existing idempotency-key material (`source_ref_hash`, converter name,
   output-affecting config snapshot) stays intact.
   The ownership dimension is added by lookup and uniqueness scope, not by changing
   the hash payload.

3. **Trusted KaraKeep matching is parsed-origin exact.**
   The carve-out applies only when the candidate URL's `(scheme, hostname, port)`
   exactly matches `karakeep_base_url` and the path is under
   `/api/v1/assets/`.
   Lookalike hosts, suffixes, prefixes, and same-origin non-asset URLs do not
   qualify.

## Schema Impact

OpenAPI remains unchanged.
These are authorization and fetch-policy semantics, not request/response shape
changes.

Database shape is expected to change for idempotency safety:

- Replace the current global unique/index shape on
  `conversion_jobs.idempotency_key` with an owner-scoped uniqueness shape on
  `(owner_id, idempotency_key)`.

The OpenAPI before-snapshot is captured for verification.
Database uniqueness changes are covered in the `schema-migrations` delta and its
migration tests rather than by OpenAPI diffing.

## Open Questions

None remaining at proposal time.
