# Delta for Conversion Worker

## ADDED Requirements

### Requirement: Declare KaraKeep as the authoritative raw input store

The system SHALL treat KaraKeep as the authoritative store for raw source content (HTML, text, and PDF assets) and SHALL record the KaraKeep bookmark identifier as the durable provenance reference for every conversion artifact.
Local copies of raw bytes are not required provided KaraKeep access is stable and the identifier is persisted.

#### Scenario: Provenance reference recorded for every bookmark

- **GIVEN** a bookmark is registered for conversion
- **WHEN** the bookmark record is created or looked up
- **THEN** the KaraKeep bookmark identifier is persisted as the stable provenance reference linking
  every derived artifact back to its authoritative source

#### Scenario: Raw bytes not stored locally

- **GIVEN** source content is fetched from KaraKeep for conversion
- **WHEN** the conversion completes and artifacts are uploaded
- **THEN** the raw HTML, text, or PDF bytes are not persisted beyond the ephemeral workspace; the
  KaraKeep identifier is sufficient as the durable raw-input reference

### Requirement: Include picture description capability in the idempotency key

The system SHALL include whether picture description is enabled (derived from the presence of a
configured chat completions endpoint) as an input to the idempotency key, so that jobs processed
with and without LLM figure descriptions produce distinct keys.

#### Scenario: Key differs when picture description enabled vs disabled

- **GIVEN** two conversion submissions for the same bookmark with identical Docling config and
  payload version
- **WHEN** one submission has a chat completions endpoint configured and the other does not
- **THEN** the two submissions produce different idempotency keys and are treated as distinct jobs

#### Scenario: Key stable when picture description capability unchanged

- **GIVEN** a resubmission with the same bookmark, Docling config, payload version, and picture
  description capability flag
- **WHEN** the idempotency key is computed
- **THEN** the key matches the existing job and the submission is rejected as a duplicate

### Requirement: Persist conversion config in the manifest

The system SHALL write the full Docling configuration snapshot used for a conversion into the S3
manifest, so the conversion can be replayed with identical parameters.

#### Scenario: Manifest contains Docling config snapshot

- **GIVEN** a conversion completes successfully
- **WHEN** the manifest is written to the ephemeral workspace
- **THEN** the manifest includes all Docling configuration fields (OCR settings, table structure,
  VLM model, page limit, picture timeout) and the picture description enabled flag as a config
  snapshot section

#### Scenario: Config snapshot matches idempotency key inputs

- **GIVEN** a manifest is present for a completed conversion
- **WHEN** the config snapshot is read from the manifest
- **THEN** the fields present are exactly those used to compute the idempotency key, enabling
  exact replay

## MODIFIED Requirements

### Requirement: Create conversion jobs with idempotency protection

The system SHALL create a conversion job record with a computed idempotency key and SHALL reject submissions whose key matches an existing record. (Previously: idempotency key was a hash of `aizk_uuid + payload_version + docling_version + config_hash` covering only Docling settings.
Now: key also includes `picture_description_enabled`, a boolean derived from whether a chat completions endpoint is configured.)

### Requirement: Create a conversion output record on success

The system SHALL create a conversion output record capturing artifact locations, content hash,
figure count, pipeline metadata, Docling version, and the config snapshot used for the conversion
on successful job completion. (Previously: output record captured artifact locations, content hash,
figure count, pipeline metadata, and Docling version only; config snapshot was not persisted.)
