# Design: Manifest Snapshot Field Scoping

## Context

This change is spec-only.
The implementation that satisfies the tightened contract already lives in [src/aizk/conversion/utilities/hashing.py](../../../src/aizk/conversion/utilities/hashing.py) as an explicit exclusion set (`_OUTPUT_IRRELEVANT_DOCLING_FIELDS`) consumed by `_docling_config_payload`, and the `ManifestConfigSnapshot` Pydantic model in [src/aizk/conversion/storage/manifest.py](../../../src/aizk/conversion/storage/manifest.py) enforces the seven-field contract at validation time via `extra="forbid"`.
The spec was lagging that enforcement: it described the included set in terms of a field-name prefix that, after the 2026-04-08 docling-config-clarity rename, no longer matches the intent.

## Decisions

### Decision: Principle-based requirement, field names only in scenarios

**Chosen:** Phrase the inclusion rule as a principle ("if and only if the field's value affects replayable output") in the requirement body, and move the specific field names (`docling_picture_description_base_url`, `docling_picture_description_api_key`) into scenarios as concrete illustrations.

**Rationale:** A requirement that enumerates today's excluded fields is myopic — a future provider-identity or transport-only field would either silently widen the contract or force a spec edit.
Stating the property of the excluded fields keeps the contract stable across future additions and forces each new field to be classified against an existing rule rather than added to a list.
Scenarios carry concreteness for verification; the requirement carries the general claim.

**Alternatives considered:**

- **Enumerate excluded fields in the requirement.**
  Clear and testable but locks the spec to the current field set; any new `docling_` field requires a spec edit regardless of whether its semantics are novel.
- **Leave the requirement wording at "all `docling_`-prefixed fields" and rely on `ManifestConfigSnapshot`'s `extra="forbid"` as the enforcement mechanism.**
  The Pydantic model is a mechanism detail and is not a contract; a future change could relax it without a spec violation.
  The spec must state the property independently.

### Decision: Independent secrets-persistence prohibition

**Chosen:** Add a second, independent sentence to the manifest-persistence requirement: the system SHALL NOT persist any credential, secret, or access token into the manifest, regardless of whether it is judged "output-affecting."

**Rationale:** Layering two prohibitions protects against a single failure mode where a future reader redefines "output-affecting" to include a credential (e.g., to force re-hashing when a key rotates).
Even under that misreading, secrets are still excluded from durable artifact storage.
This is defense-in-depth at the contract level, matching the code-level `extra="forbid"` posture of `ManifestConfigSnapshot`.

**Alternatives considered:**

- **Single "output-affecting" rule.**
  Sufficient today but relies on every future author correctly classifying credentials as non-output-affecting.
  The security property is too important to depend on a transitive inference.

## Risks

- **Sync drift with Technical Notes.**
  The baseline spec's `## Technical Notes` section contains two sentences (the "Idempotency key" and "Manifest config_snapshot" bullets) that restate the old "all `docling_`-prefixed fields" wording.
  After `sdd-sync` merges this delta, those bullets must also be updated or removed; otherwise the baseline contradicts itself.
  Mitigation: add a task to rewrite both Technical Notes bullets as part of this change, so the sync step picks them up.
- **Scenario brittleness to new fields.**
  The "Manifest contains Docling config snapshot" scenario enumerates current output-affecting fields.
  Adding a new output-affecting `docling_` field will require updating this scenario.
  Mitigation: acceptable — the scenario is evidence, not the contract, and enumerating them provides concreteness for verification.
  The requirement itself remains stable.
