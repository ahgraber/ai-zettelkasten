# Delta for Conversion API

## MODIFIED Requirements

### Requirement: Expose readiness probe with dependency checks

The system SHALL expose a readiness endpoint that validates database connectivity, S3 reachability, and — when the picture description endpoint is configured — picture description endpoint reachability.
When the picture description endpoint is configured, the readiness probe SHALL issue `GET {base_url}/models` with an `Authorization: Bearer` header and a 5-second timeout.
If the picture description endpoint is not configured, it is omitted from the check results entirely.
The endpoint returns HTTP 200 when all included checks pass and HTTP 503 when any included check fails.

(Previously: readiness probe checked only database and S3; no picture description check)

#### Scenario: Picture description endpoint included when configured

- **GIVEN** `DOCLING_PICTURE_DESCRIPTION_BASE_URL` and `DOCLING_PICTURE_DESCRIPTION_API_KEY` are set
- **WHEN** a client requests `/health/ready`
- **THEN** the response includes a `picture_description` check result alongside `database` and `s3`

#### Scenario: Picture description check fails after startup

- **GIVEN** the picture description endpoint was reachable at startup but is now unreachable
- **WHEN** a client requests `/health/ready`
- **THEN** the `picture_description` check result has status `"unavailable"`, the overall response status is `"unavailable"`, and the HTTP status is 503

#### Scenario: Picture description omitted when not configured

- **GIVEN** `DOCLING_PICTURE_DESCRIPTION_BASE_URL` is not set
- **WHEN** a client requests `/health/ready`
- **THEN** the response contains only `database` and `s3` check results, with no `picture_description` entry
