# Delta for Conversion Worker

> Generated from code analysis on 2026-03-22
> Source files: src/aizk/conversion/workers/worker.py, src/aizk/conversion/storage/s3_client.py,
> src/aizk/conversion/datamodel/output.py

## MODIFIED Requirements

### Requirement: Skip S3 overwrite when content hash matches

The system SHALL compare the new content hash against the most recent conversion output **for the
same bookmark** and reuse the existing S3 location if the hashes match. *(Previously: the upload
path was unconditional — no comparison against prior output was performed.)*

#### Scenario: Matching hash reuses existing artifacts

- **GIVEN** a reprocessed bookmark produces Markdown with the same content hash as the previous output
- **WHEN** the worker completes conversion
- **THEN** the existing S3 artifacts are reused and a new output record is created pointing to the
  existing location without overwriting

#### Scenario: Changed hash overwrites artifacts

- **GIVEN** a reprocessed bookmark produces Markdown with a different content hash
- **WHEN** the worker completes upload
- **THEN** S3 artifacts are overwritten and a new output record is created

#### Scenario: No prior output skips hash comparison

- **GIVEN** the bookmark has no prior succeeded conversion output
- **WHEN** the worker completes conversion
- **THEN** the worker proceeds with a full upload without attempting a hash comparison
