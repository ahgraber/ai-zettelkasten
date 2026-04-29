# Expected Schema Changes: network-egress-policy

This change introduces no OpenAPI surface modifications.
All work is internal to fetcher and converter behavior — egress validation, redirect handling, image pre-fetch into the workspace, and subprocess-metadata containment checks.
No new endpoints, no new request or response shapes, no new error-response codes, no field renames or removals.

## OpenAPI

No changes expected.

`sdd-verify` SHOULD confirm an empty diff between
`.specs/changes/network-egress-policy/schemas/before/conversion-api-openapi.json`
and the after-snapshot generated at verify time.
