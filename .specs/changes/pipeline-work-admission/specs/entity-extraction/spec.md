# Delta for Entity Extraction

## ADDED Requirements

### Requirement: Extraction declares its pending work over chunking state

The mention-extraction stage SHALL declare a pending-work derivation in which a source is pending exactly when it has an active chunking run and no extraction work-unit.
The derivation SHALL NOT mark pending any source that already has an extraction work-unit, whatever that unit's status: re-extraction after an upstream-generation, extractor, or input-policy change remains outside admission, reachable only through the stage's confirmation-gated reprocessing entry points.

Serves: automatic-graph-admission, new-stage-absorbs-corpus

#### Scenario: A chunked but never-extracted source is pending

- **GIVEN** a source with an active chunking run and no extraction work-unit
- **WHEN** the pending set is evaluated
- **THEN** the source is pending

#### Scenario: A source with an extraction unit is not pending

- **GIVEN** a source with an extraction work-unit in any status
- **WHEN** the pending set is evaluated
- **THEN** the source is not pending

#### Scenario: A re-chunked extracted source is not re-admitted

- **GIVEN** a source whose chunking was superseded by a new active run after its extraction work-unit succeeded
- **WHEN** the pending set is evaluated
- **THEN** the source is not pending

#### Scenario: A source without a chunking run is not pending

- **GIVEN** a source with no chunking run
- **WHEN** the pending set is evaluated
- **THEN** the source is not pending

### Requirement: A stale extraction is identifiable

For any source with extraction output, the stage SHALL identify the source as stale when re-running extraction would read different inputs than its active extraction run recorded — when the upstream state it consumed has been superseded.
Staleness SHALL NOT make the source pending: no staleness condition triggers automatic re-admission.

Serves: visible-stage-coverage, bounded-inference-spend

#### Scenario: A re-chunked source is stale

- **GIVEN** a source extracted from a chunking generation later superseded by a new active chunking run
- **WHEN** staleness is evaluated
- **THEN** the source is stale

#### Scenario: A newly contextualized source is stale

- **GIVEN** a source extracted from raw chunk text before an active contextualization run later provided variants for its chunks
- **WHEN** staleness is evaluated
- **THEN** the source is stale

#### Scenario: A current source is not stale

- **GIVEN** a source whose active extraction run consumed the source's current active upstream state
- **WHEN** staleness is evaluated
- **THEN** the source is not stale

### Requirement: Re-admission of an extracted source is explicit and operator-initiated

The stage SHALL declare a re-admission action through which an operator requeues a source's extraction; after the action, the source SHALL have a queued extraction work-unit, and its processing SHALL be a fresh extraction reading the source's current active inputs, superseding the prior output under the existing supersession contract.
Eligibility SHALL be limited to work-units in a terminal status whose source is stale.
Re-admission SHALL occur only through explicit operator action.

Serves: visible-stage-coverage, bounded-inference-spend

#### Scenario: A re-admitted stale source is re-extracted against current inputs

- **GIVEN** a stale source with a succeeded extraction work-unit
- **WHEN** an operator applies the re-admission action
- **THEN** the source has a queued extraction work-unit, and its processing reads the current active inputs and supersedes the prior output

#### Scenario: A non-stale unit is ineligible for re-admission

- **GIVEN** a terminal extraction work-unit whose source is not stale
- **WHEN** re-admission is attempted on it
- **THEN** the unit is skipped as ineligible and its status is unaltered
