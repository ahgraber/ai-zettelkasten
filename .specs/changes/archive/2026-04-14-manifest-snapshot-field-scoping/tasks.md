# Tasks: Manifest Snapshot Field Scoping

## Implementation

- [x] Add `_OUTPUT_IRRELEVANT_DOCLING_FIELDS` exclusion set in [src/aizk/conversion/utilities/hashing.py](../../../src/aizk/conversion/utilities/hashing.py) and filter it out of `_docling_config_payload`.
- [x] Update the `_expected_key` helper in [tests/conversion/unit/test_hashing.py](../../../tests/conversion/unit/test_hashing.py) to mirror the exclusion so the reference computation matches the production hash.

## Test Coverage

- [x] Add a unit test asserting that `compute_idempotency_key` returns the same hash for two `ConversionConfig` instances that differ only in `docling_picture_description_base_url` (both non-empty, both with api_key configured).
  Covers the "Key stable when only the picture-description endpoint URL or API key rotates" scenario.
- [x] Add a unit test asserting that `compute_idempotency_key` returns the same hash for two `ConversionConfig` instances that differ only in `docling_picture_description_api_key` (both non-empty, both with base_url configured).
  Covers the same scenario for the credential axis.
- [x] Confirm that [tests/conversion/unit/test_hashing.py::test_build_output_config_snapshot_matches_manifest_contract](../../../tests/conversion/unit/test_hashing.py) already covers the "Manifest omits picture-description provider identity and credentials" scenario (the asserted 7-field set excludes both); if not, extend it with an explicit negative assertion.
  Extended with `test_build_output_config_snapshot_omits_provider_identity_and_credentials` which configures both fields to non-empty values and asserts they are absent from the snapshot.

## Spec Sync

- [x] During `sdd-sync`, update the two Technical Notes bullets in [.specs/specs/conversion-worker/spec.md](../../../.specs/specs/conversion-worker/spec.md) that restate the old contract:
  \- the "Idempotency key" bullet ("`config_hash` includes all `docling_`-prefixed fields …") — rewrite to match the new principle (output-affecting fields only).
  \- the "Manifest config_snapshot" bullet ("manifest includes a `config_snapshot` section with all Docling config fields …") — rewrite to match the new principle and note the secrets-persistence prohibition.
- [x] During `sdd-sync`, merge the delta's two MODIFIED Requirements and the new scenarios into the baseline `specs/conversion-worker/spec.md` Requirements section.
