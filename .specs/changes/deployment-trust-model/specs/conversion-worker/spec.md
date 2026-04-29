# Delta for conversion-worker

## MODIFIED Requirements

### Requirement: Create a conversion output record on success

The existing requirement is extended with one additional persistence clause:

The new conversion output row SHALL carry an `owner_id` value equal to the parent Job's `owner_id`.
The worker SHALL NOT resolve a Principal independently; ownership flows from the API → Job → Output chain because the worker has no inbound request and therefore no Principal in its execution context.
The worker SHALL read `Job.owner_id` and copy it onto the new `conversion_outputs` row at insert time as a single read-and-copy operation; the worker SHALL NOT mutate `Job.owner_id` (Job is immutable for ownership attribution after API materialization).

(Previously: outputs were persisted without an `owner_id` column.
The change adds the column and ties its value to the parent Job's owner; the existing artifact-location, content-hash, figure-count, and pipeline-metadata clauses are unchanged.)

#### Scenario: Output owner_id copied from parent Job

- **GIVEN** a Job with `owner_id = "self"` reaches successful completion
- **WHEN** the worker creates the conversion output record
- **THEN** the new `conversion_outputs` row has `owner_id = "self"`, matching the parent Job's owner

#### Scenario: Worker does not mutate Job.owner_id

- **GIVEN** a Job with `owner_id = "self"` is processed by the worker
- **WHEN** the worker writes any of its mutable metadata columns (status, attempt count, error message, output reference)
- **THEN** the Job's `owner_id` value is unchanged, consistent with the existing Source-identity immutability invariant
