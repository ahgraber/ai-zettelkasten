# Delta for Conversion Worker

## MODIFIED Requirements

### Requirement: Create conversion jobs with idempotency protection

The system SHALL assign each conversion job an idempotency key that is stable across resubmissions with identical contributing inputs and distinct whenever any contributing input differs, and SHALL reject submissions whose key matches an existing record.
Contributing inputs are: the internal bookmark identifier, the payload version, the Docling version, the Docling configuration fields that affect replayable output, and whether picture description is enabled.
A Docling configuration field contributes to the key if and only if its value affects replayable output; fields that only identify an external provider, authenticate to one, or control transport behavior without affecting output SHALL NOT contribute.

Previously the contributing inputs included "all `docling_`-prefixed Docling configuration fields (including `docling_enable_picture_classification`)".
That wording was correct when introduced but became ambiguous once additional non-output-affecting fields acquired the `docling_` prefix.
The requirement is restated as a principle about output-affecting inputs, so the set can evolve without re-opening the contract.

#### Scenario: Key stable when only the picture-description endpoint URL or API key rotates

- **GIVEN** two submissions with identical bookmark, payload version, Docling version, Docling output-affecting configuration, and picture-description enablement
- **WHEN** the two submissions differ only in the value of `DOCLING_PICTURE_DESCRIPTION_BASE_URL` or `DOCLING_PICTURE_DESCRIPTION_API_KEY` (with both still configured in each case)
- **THEN** the two submissions produce the same idempotency key and the second is rejected as a duplicate

### Requirement: Persist conversion config in the manifest

The system SHALL write the Docling configuration fields that affect replayable output into the S3 manifest as a `config_snapshot` section, so the conversion can be replayed with identical parameters.
A Docling configuration field appears in `config_snapshot` if and only if its value affects replayable output; provider-identity fields, credentials, and transport-only controls SHALL NOT appear.
Independent of the replay criterion, the system SHALL NOT persist any credential, secret, or access token into the manifest; secrets MUST NOT be written to durable artifact storage under any circumstance.

Previously the requirement said the system SHALL write the full Docling configuration snapshot into the S3 manifest.
The word "full" became ambiguous once non-output-affecting and credential fields acquired the `docling_` prefix.
The requirement now states the inclusion principle and adds an independent secrets-persistence prohibition, so the security invariant cannot be lost by a future redefinition of replayability.

#### Scenario: Manifest contains Docling config snapshot

- **GIVEN** a conversion completes successfully
- **WHEN** the manifest is written to the ephemeral workspace
- **THEN** the manifest includes the Docling configuration fields that affect replayable output (OCR settings, table structure, picture description model (`docling_picture_description_model`), page limit, picture timeout, picture classification enabled) and the picture description enabled flag as a `config_snapshot` section

#### Scenario: Manifest captures picture classification flag

- **GIVEN** a conversion completes with `docling_enable_picture_classification=True`
- **WHEN** the manifest is written
- **THEN** the `config_snapshot` section includes `"docling_enable_picture_classification": true`

#### Scenario: Config snapshot matches idempotency key inputs

- **GIVEN** a manifest is present for a completed conversion
- **WHEN** the config snapshot is read from the manifest
- **THEN** the fields present are exactly those used to compute the idempotency key, enabling exact replay

#### Scenario: Manifest omits picture-description provider identity and credentials

- **GIVEN** a conversion completes successfully with `DOCLING_PICTURE_DESCRIPTION_BASE_URL` and `DOCLING_PICTURE_DESCRIPTION_API_KEY` both configured to non-empty values
- **WHEN** the `config_snapshot` section is read from the manifest
- **THEN** the section contains no entry for the picture-description endpoint URL and no entry for the picture-description API key, illustrating the general rule that provider identity and credentials are not persisted
