# Tasks: conversion-provenance-idempotency

## Idempotency Key

- [x] Add `picture_description_enabled: bool` parameter to `compute_idempotency_key` in
  `src/aizk/conversion/utilities/hashing.py`
- [x] Update the key hash string to append `:{picture_description_enabled}` after the existing
  fields
- [x] Derive `picture_description_enabled` from `config.chat_completions_base_url is not None`
  at the call site in `src/aizk/conversion/api/routes/jobs.py`
- [x] Update unit tests for `compute_idempotency_key` to cover enabled/disabled cases producing
  distinct keys and identical inputs producing the same key

## Manifest

- [x] Add a `config_snapshot` field to the manifest data model in
  `src/aizk/conversion/storage/manifest.py` containing the `docling_*` config fields and
  `picture_description_enabled`
- [x] Populate `config_snapshot` when building the manifest, passing the relevant config fields
  and the `picture_description_enabled` flag
- [x] Update manifest serialisation tests to assert `config_snapshot` is present and contains
  the correct fields

## Spec Technical Notes

- [x] Update the `## Technical Notes` section of `.specs/specs/conversion-worker/spec.md` to:
  - Change the idempotency key note to: hash of `aizk_uuid + payload_version + docling_version + config_hash + picture_description_enabled`
  - Add a note: "KaraKeep is the authoritative store for raw source content; `karakeep_id` is the durable provenance reference.
    Raw bytes are not archived locally."
  - Add a note: "Manifest includes a `config_snapshot` section with all Docling config fields and
    `picture_description_enabled` to enable exact replay."
