# Delta for graph-work-intake

## ADDED Requirements

### Requirement: Intake availability is independent of automatic admission

Intake SHALL accept submissions whenever the graph service is up, regardless of whether automatic admission is enabled for the target stage.
The admission enablement flags govern only the background admission loop; they are not an intake switch.

Serves: trustworthy-operator-view

#### Scenario: A submission succeeds while automatic admission is disabled

- **GIVEN** a stage with automatic admission not enabled and work with no existing work-unit
- **WHEN** the work is submitted through intake
- **THEN** the work-unit is created

## MODIFIED Requirements

### Requirement: The graph service accepts work submissions for its stages

> Previously: the requirement did not state that the response distinguishes a created unit from a reused one.

The graph service SHALL expose a submission operation for each graph processing stage: `POST /v1/contextualizations` accepting a conversion-output reference, and `POST /v1/extractions` accepting a source identity.
A successful submission SHALL create the stage's work-unit, or return the existing unit when the submission resolves to work already enqueued, and the resulting unit SHALL be identical to one created through the stage's domain enqueue for the same work.
The response SHALL distinguish the two outcomes: HTTP 201 when the submission created the unit, HTTP 200 when it returned an existing unit.

Serves: trustworthy-operator-view

#### Scenario: A new submission creates the work-unit

- **GIVEN** work with no existing work-unit at the target stage
- **WHEN** it is submitted through the stage's intake operation
- **THEN** the stage's work-unit exists, queued for processing, and the response is HTTP 201

#### Scenario: A resubmission returns the existing unit

- **GIVEN** work already submitted
- **WHEN** the same work is submitted again
- **THEN** the original work-unit is returned with HTTP 200 and no duplicate is created

#### Scenario: An intake unit equals a domain-enqueued unit

- **GIVEN** the same work
- **WHEN** it is submitted through intake in one case and enqueued through the stage's domain path in the other
- **THEN** the resulting work-units are identical and downstream processing cannot distinguish them
