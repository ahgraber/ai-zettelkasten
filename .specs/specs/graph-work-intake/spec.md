# Graph Work Intake Specification

> Synced from change `pipeline-work-admission` on 2026-08-27

## Purpose

The graph-work-intake capability lets the graph service be asked to create work, as the conversion service already can.
Each graph processing stage exposes a submission operation that resolves an upstream reference — a conversion output for contextualization, a source identity for extraction — and creates the stage's work-unit through that stage's own enqueue.
A submission resolving to work already enqueued returns the existing unit rather than duplicating it, and the unit intake creates is indistinguishable from one the stage's domain path created for the same work.

Intake owns no throttle of its own: the capacity limit lives at the enqueue seam, so intake is bound by it exactly as an admission pass or a backfill command is.
What intake does own is the shape of the refusal, which matches the conversion service's — HTTP 503 carrying `Retry-After` — so one backoff convention covers the fleet and an operator learns one set of controls rather than a different shape per stage.

This capability establishes the graph work-intake contract.
It does not retroactively specify the graph service's existing job read, retry, and cancel operations, which carry no baseline requirements.

## Requirements

### Requirement: The graph service accepts work submissions for its stages

The graph service SHALL expose a submission operation for each graph processing stage: `POST /v1/contextualizations` accepting a conversion-output reference, and `POST /v1/extractions` accepting a source identity.
A successful submission SHALL create the stage's work-unit, or return the existing unit when the submission resolves to work already enqueued, and the resulting unit SHALL be identical to one created through the stage's domain enqueue for the same work.

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

#### Scenario: An unknown reference is rejected cleanly

- **GIVEN** a submission naming a conversion output or source that does not exist
- **WHEN** the submission is made
- **THEN** it is rejected as not found and no work-unit exists for it

## Technical Notes

- **Implementation**: `src/aizk/graph/api/routes/__init__.py` (`POST /v1/contextualizations`) and `src/aizk/graph/api/routes/extraction.py` (`POST /v1/extractions`); request and response models in `src/aizk/graph/api/schemas.py`; the admission settings dependency in `src/aizk/graph/api/dependencies.py`.
- **Request ordering**: each route mirrors conversion's submission ordering — resolve the upstream reference, look up an existing unit (200), evaluate capacity, create (201) — inside one write transaction it commits.
- **Rejection shape**: the refusal reuses the conversion service's `QueueFullResponse` model rather than a graph-local copy, so the two services cannot drift apart in body shape.
- **Principal handling**: intake adopts the graph service's existing principal handling unchanged; it introduces no authorization model of its own.
  The deployment treats the network as the authorization boundary, and spend exposure is bounded by the stage's capacity limit and by automatic admission being off by default — see the change `pipeline-work-admission` design.
- **Schema**: the graph API is tracked as `graph-api-openapi` in `.specs/.sdd/schema-config.yaml`; both submissions declare the `200`, `201`, `404`, `422`, and `503` responses they can return, with `Retry-After` declared on the `503`.
