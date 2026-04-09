# Delta for Conversion Worker

## MODIFIED Requirements

### Requirement: Load configuration from environment variables

The system SHALL load all configuration from environment variables with sensible defaults for local development.
The picture description endpoint is configured via `DOCLING_PICTURE_DESCRIPTION_BASE_URL`, `DOCLING_PICTURE_DESCRIPTION_API_KEY`, and `DOCLING_PICTURE_DESCRIPTION_MODEL` (previously `CHAT_COMPLETIONS_BASE_URL`, `CHAT_COMPLETIONS_API_KEY`, and `DOCLING_VLM_MODEL`).

(Previously: picture description endpoint configured via `CHAT_COMPLETIONS_BASE_URL`, `CHAT_COMPLETIONS_API_KEY`, and `DOCLING_VLM_MODEL`)

### Requirement: Convert documents to Markdown and extract figures

When picture description is enabled and classification is active, the enrichment loop calls the VLM API configured via `DOCLING_PICTURE_DESCRIPTION_BASE_URL` and `DOCLING_PICTURE_DESCRIPTION_MODEL`.

(Previously: enrichment loop called the VLM API configured via `CHAT_COMPLETIONS_BASE_URL` and `DOCLING_VLM_MODEL`)

### Requirement: Include picture description capability in the idempotency key

The system SHALL include whether picture description is enabled (derived from the presence of a configured `DOCLING_PICTURE_DESCRIPTION_BASE_URL` and `DOCLING_PICTURE_DESCRIPTION_API_KEY`) as an input to the idempotency key.

(Previously: derived from presence of `CHAT_COMPLETIONS_BASE_URL` and `CHAT_COMPLETIONS_API_KEY`)

### Requirement: Persist conversion config in the manifest

The config snapshot section of the manifest SHALL record `docling_picture_description_model` (previously `docling_vlm_model`) alongside the other Docling configuration fields.

(Previously: manifest recorded `docling_vlm_model`)

### Requirement: Validate required external services on startup

The system SHALL probe the picture description endpoint at startup when `DOCLING_PICTURE_DESCRIPTION_BASE_URL` and `DOCLING_PICTURE_DESCRIPTION_API_KEY` are both set.
The probe SHALL issue `GET {base_url}/models` with an `Authorization: Bearer` header and a 10-second timeout.
A non-2xx response or connection error SHALL raise `StartupValidationError` and prevent startup.
If neither field is set, the probe is a no-op.

(Previously: no probe for the picture description endpoint; only S3 and KaraKeep were probed)

#### Scenario: Picture description endpoint reachable at startup

- **GIVEN** `DOCLING_PICTURE_DESCRIPTION_BASE_URL` and `DOCLING_PICTURE_DESCRIPTION_API_KEY` are configured
- **WHEN** the worker or API process starts
- **THEN** `GET {base_url}/models` is called with an Authorization header and the process continues if it returns 2xx

#### Scenario: Picture description endpoint unreachable at startup

- **GIVEN** `DOCLING_PICTURE_DESCRIPTION_BASE_URL` is configured but the endpoint is unreachable or returns non-2xx
- **WHEN** the worker or API process starts
- **THEN** the process logs a structured error identifying the failure and exits with a non-zero exit code

#### Scenario: Picture description not configured — probe skipped

- **GIVEN** `DOCLING_PICTURE_DESCRIPTION_BASE_URL` is not set
- **WHEN** the worker or API process starts
- **THEN** no probe is made for the picture description endpoint and startup proceeds normally

### Requirement: Log optional feature status summary on startup

The system SHALL log a structured summary of all optional feature states on startup.
When picture descriptions are disabled due to missing configuration, the reason string SHALL be `"DOCLING_PICTURE_DESCRIPTION_BASE_URL not configured"`.
Picture classification reports as enabled only when both `DOCLING_ENABLE_PICTURE_CLASSIFICATION=true` and `DOCLING_PICTURE_DESCRIPTION_BASE_URL` is configured.

(Previously: reason string was `"chat completions endpoint not configured"`; picture classification check referenced `CHAT_COMPLETIONS_BASE_URL`)

#### Scenario: Optional feature disabled due to missing config

- **GIVEN** `DOCLING_PICTURE_DESCRIPTION_BASE_URL` or `DOCLING_PICTURE_DESCRIPTION_API_KEY` is not configured
- **WHEN** the process starts
- **THEN** the startup summary log entry lists picture descriptions as disabled with reason `"DOCLING_PICTURE_DESCRIPTION_BASE_URL not configured"`
