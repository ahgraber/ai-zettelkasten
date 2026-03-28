# Delta for Conversion API

> Generated from code analysis on 2026-03-22; extended with content endpoints on 2026-03-23
> Source files: src/aizk/conversion/api/routes/jobs.py, src/aizk/conversion/api/schemas/jobs.py, src/aizk/conversion/api/main.py, src/aizk/conversion/datamodel/output.py, src/aizk/conversion/storage/s3_client.py

## ADDED Requirements

### Requirement: Retrieve conversion outputs for a bookmark

The system SHALL expose an endpoint returning all conversion output records for a bookmark ordered by creation time descending, with an option to return only the most recent output.

#### Scenario: Retrieve all outputs

- **GIVEN** a bookmark has multiple successful conversions
- **WHEN** a client requests outputs for the bookmark's internal identifier
- **THEN** all conversion output records are returned ordered by creation time descending

#### Scenario: Retrieve latest output only

- **GIVEN** a bookmark has multiple successful conversions
- **WHEN** a client requests outputs with the latest flag set
- **THEN** only the most recently created conversion output record is returned

### Requirement: Serve raw manifest JSON for a conversion output

The system SHALL expose an endpoint that retrieves and returns the raw manifest JSON for a conversion output record directly from object storage without re-parsing or transforming the content.

#### Scenario: Retrieve manifest for a known output

- **GIVEN** a conversion output record exists with a valid manifest key
- **WHEN** a client requests the manifest by output identifier
- **THEN** the system returns the raw manifest bytes with Content-Type `application/json`

#### Scenario: Manifest object missing from storage

- **GIVEN** a conversion output record exists but its manifest object is absent from storage
- **WHEN** a client requests the manifest
- **THEN** the system returns a 404 response

### Requirement: Serve markdown content for a conversion output

The system SHALL expose an endpoint that retrieves and returns the converted markdown text for a conversion output record directly from object storage.

#### Scenario: Retrieve markdown for a known output

- **GIVEN** a conversion output record exists with a valid markdown key
- **WHEN** a client requests the markdown by output identifier
- **THEN** the system returns the markdown bytes with Content-Type `text/markdown; charset=utf-8`

### Requirement: Serve figure images for a conversion output

The system SHALL expose an endpoint that retrieves and returns individual figure images for a conversion output record by filename, and SHALL reject filenames that could escape the figures storage prefix.

#### Scenario: Retrieve a valid figure

- **GIVEN** a conversion output record exists and a figure with the requested filename is present in object storage
- **WHEN** a client requests the figure by output identifier and bare filename
- **THEN** the system returns the figure bytes with an appropriate image Content-Type

#### Scenario: Reject path-traversal filename

- **GIVEN** a client submits a filename containing `/` or an empty filename
- **WHEN** the API receives the request
- **THEN** the system returns a 4xx error response without accessing object storage

#### Scenario: Output has no figures

- **GIVEN** a conversion output record with `figure_count == 0`
- **WHEN** a client requests any figure by filename
- **THEN** the system returns a 404 response

### Requirement: Return structured error responses for storage failures

The system SHALL return a 502 response when object storage returns an unexpected error, and a 404 response when the requested object key does not exist.

#### Scenario: Object storage error on content fetch

- **GIVEN** object storage returns an error other than key-not-found
- **WHEN** a client requests any content endpoint
- **THEN** the system returns a 502 response

## MODIFIED Requirements

### Requirement: Retry failed jobs

The system SHALL expose an endpoint to retry a failed or permanently failed job by resetting its status to QUEUED and incrementing its attempt count. (Previously: the retry implementation resets status and clears scheduling timestamps but does not increment `attempts`.)

#### Scenario: Retry a failed-retryable job

- **GIVEN** a job has status FAILED_RETRYABLE or FAILED_PERM
- **WHEN** a client posts a retry request for that job
- **THEN** the job status resets to QUEUED, the attempt count increments by one, and the retry scheduling timestamp is cleared
