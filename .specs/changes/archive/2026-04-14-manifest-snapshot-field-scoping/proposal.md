# Proposal: Manifest Snapshot Field Scoping

## Intent

The conversion-worker spec describes the idempotency key and the manifest `config_snapshot` in terms of "all `docling_`-prefixed fields."
That wording was written when the picture-description endpoint URL and API key lived under `CHAT_COMPLETIONS_*` env vars and therefore did not match the `docling_` prefix.
After the 2026-04-08 docling-config-clarity rename, both fields now begin with `docling_` and are structurally indistinguishable from output-affecting fields.
The spec should state the intended contract explicitly — the snapshot captures fields that affect replayable output, and must exclude the endpoint URL and API key — so a future field addition cannot silently widen the idempotency key or leak a secret into `manifest.json`.

## Scope

**In scope:**

- Clarify the idempotency-key requirement in `specs/conversion-worker/spec.md` to state which `docling_*` fields are excluded and why (endpoint identity, credentials).
- Clarify the manifest `config_snapshot` requirement to match the already-enumerated field list and to explicitly exclude provider-endpoint and credential fields.
- Add a scenario asserting that the manifest `config_snapshot` does not contain the picture-description base URL or API key.

**Out of scope:**

- Any change to which fields are currently captured (the implementation already excludes them after the companion bugfix; see [hashing.py](../../../src/aizk/conversion/utilities/hashing.py)).
- Expanding the captured field set, or adding any new `docling_*` configuration.
- Changes to the idempotency-key hash format, ordering, or algorithm.
- Changes to the `ManifestConfigSnapshot` Pydantic model or its `extra="forbid"` posture.

## Approach

Two requirement sentences in `specs/conversion-worker/spec.md` (the idempotency-key bullet and the manifest `config_snapshot` bullet) are tightened from "all `docling_`-prefixed fields" to "the `docling_`-prefixed fields that affect replayable output (excludes the picture-description endpoint URL and API key)."
One new scenario under the existing manifest Requirement asserts the exclusion as an observable contract so regressions surface in tests rather than during upload.

## Schema Impact

None.
This change touches spec prose and adds one scenario.
It does not add, modify, or remove any API endpoint, request, or response schema.
