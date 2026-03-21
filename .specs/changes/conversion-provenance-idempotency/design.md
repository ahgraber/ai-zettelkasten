# Design: conversion-provenance-idempotency

## Context

The conversion worker already computes an idempotency key from `aizk_uuid + payload_version + docling_version + config_hash`, where `config_hash` covers only the `docling_*` config fields.
The manifest already records `docling_version` and `pipeline_name` but not the full config.
The `chat_completions_base_url` setting is used at conversion time to enable LLM picture descriptions but is absent from both the idempotency key and the manifest.

KaraKeep is the managed upstream system; the worker fetches fresh content per job and discards raw bytes after upload.
The `karakeep_id` is already stored on the `Bookmark` record as a unique key but is not explicitly documented as the provenance reference in the spec.

## Decisions

### Decision: Represent chat completions presence as a boolean flag in the key

**Chosen:** derive a `picture_description_enabled: bool` from `chat_completions_base_url is not None` and include it in the idempotency key hash string.

**Rationale:** The actual endpoint URL is an operational detail and may change between deployments pointing to equivalent services.
What materially affects output is whether picture description ran at all.
A boolean captures this cleanly and avoids key churn from URL normalisation edge cases.

**Alternatives considered:**

- Hash the full URL: causes key churn when the endpoint moves without affecting behaviour (e.g.
  localhost vs container hostname for the same service).
- Ignore it entirely: leaves the idempotency violation in place; two jobs with the same key can
  produce different outputs.

### Decision: Write config snapshot to manifest, not to the database output record

**Chosen:** add a `config_snapshot` section to the S3 manifest JSON containing the Docling config
fields and `picture_description_enabled`.

**Rationale:** The manifest is already the canonical artifact-level record of how a conversion was produced.
Adding the config there keeps the database schema unchanged and puts provenance data alongside the artifacts it describes.
The database output record carries enough for operational queries (hash, counts, versions); full replay parameters belong with the artifact.

**Alternatives considered:**

- Add a JSON column to `ConversionOutput`: requires a schema migration and makes the database the
  source of truth for something that is only needed for replay, not for live queries.

### Decision: No local archival of raw bytes

**Chosen:** rely on KaraKeep as the authoritative store; record `karakeep_id` as the durable provenance reference.

**Rationale:** The constitution explicitly permits raw inputs to remain in an authoritative external system when access is stable and a durable reference is recorded.
KaraKeep is the managed upstream and already stores the original HTML/PDF assets.
Duplicating bytes locally adds storage cost and operational complexity with no practical replay benefit as long as KaraKeep is available.

**Alternatives considered:**

- Archive raw bytes to S3 alongside output artifacts: adds significant storage and complicates the
  upload/workspace lifecycle; unjustified while KaraKeep remains the source of truth.

## Architecture

```text
Idempotency key inputs (hashing.py)
  aizk_uuid
  payload_version
  docling_version              (unchanged)
  docling_config_fields hash   (unchanged)
+ picture_description_enabled  (NEW: bool from chat_completions_base_url is not None)
  → SHA-256 hex digest

Manifest JSON (manifest.py)
  source_metadata   (url, normalized_url, title, source_type, fetched_at)
  conversion_metadata (job_id, payload_version, docling_version, pipeline_name, timing)
  artifacts         (s3 keys, content hash)
+ config_snapshot   (NEW: docling_* fields + picture_description_enabled)
```

## Risks

- **Manifest schema change breaks readers**: any consumer parsing the manifest will receive a new
  `config_snapshot` key; mitigated by additive-only change (new key, no removals).
- **Key churn on existing jobs**: jobs processed before this change have keys computed without `picture_description_enabled`; resubmissions after the change will produce new keys and create new jobs rather than deduplicating.
  Acceptable — prior jobs were processed under the old contract.
