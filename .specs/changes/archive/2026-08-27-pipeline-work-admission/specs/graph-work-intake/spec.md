# Delta for Graph Work Intake

## ADDED Requirements

### Requirement: The graph service accepts work submissions for its stages

The graph service SHALL expose a submission operation for each graph processing stage: `POST /v1/contextualizations` accepting a conversion-output reference, and `POST /v1/extractions` accepting a source identity.
A successful submission SHALL create the stage's work-unit, or return the existing unit when the submission resolves to work already enqueued, and the resulting unit SHALL be identical to one created through the stage's domain enqueue for the same work.

Serves: uniform-work-intake

#### Scenario: A new submission creates the work-unit

- **GIVEN** work with no existing work-unit at the target stage
- **WHEN** it is submitted through the stage's intake operation
- **THEN** the stage's work-unit exists, queued for processing

#### Scenario: A resubmission returns the existing unit

- **GIVEN** work already submitted
- **WHEN** the same work is submitted again
- **THEN** the original work-unit is returned and no duplicate is created

#### Scenario: An intake unit equals a domain-enqueued unit

- **GIVEN** the same work
- **WHEN** it is submitted through intake in one case and enqueued through the stage's domain path in the other
- **THEN** the resulting work-units are identical and downstream processing cannot distinguish them

### Requirement: Intake refuses at capacity with the fleet's rejection shape

When the target stage's declared capacity is reached, a submission of new work SHALL be refused with HTTP 503 carrying a `Retry-After` header, matching the conversion service's job-submission rejection; a submission resolving to an existing work-unit SHALL succeed regardless of capacity.

Serves: uniform-work-intake, bounded-inference-spend

#### Scenario: New work is refused at capacity

- **GIVEN** a target stage at its declared capacity
- **WHEN** new work is submitted
- **THEN** the response is HTTP 503 with a `Retry-After` header and no work-unit is created

#### Scenario: Below capacity the submission is accepted

- **GIVEN** a target stage below its declared capacity
- **WHEN** new work is submitted
- **THEN** the work-unit is created

#### Scenario: A duplicate submission bypasses the capacity refusal

- **GIVEN** a target stage at capacity and a submission resolving to an existing work-unit
- **WHEN** the submission is made
- **THEN** the existing unit is returned successfully

### Requirement: Intake validates the referenced upstream artifact

A submission referencing an upstream artifact that does not exist SHALL be rejected without creating a work-unit or any other observable state change.

Serves: uniform-work-intake

#### Scenario: An unknown reference is rejected cleanly

- **GIVEN** a submission naming a conversion output or source that does not exist
- **WHEN** the submission is made
- **THEN** it is rejected as not found and no work-unit exists for it
