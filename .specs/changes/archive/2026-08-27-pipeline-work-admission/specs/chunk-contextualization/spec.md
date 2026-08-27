# Delta for Chunk Contextualization

## ADDED Requirements

### Requirement: Contextualization declares its pending work over conversion outputs

The contextualization stage SHALL declare a pending-work derivation in which a source is pending exactly when its newest conversion output has no contextualization work-unit.
This makes a never-contextualized source pending, a source whose newest output already has a work-unit not pending, a source re-converted after its work-unit was created pending again, and a source with no conversion output not pending.

Serves: automatic-graph-admission, new-stage-absorbs-corpus

#### Scenario: A never-contextualized source is pending

- **GIVEN** a source with a conversion output and no contextualization work-unit
- **WHEN** the pending set is evaluated
- **THEN** the source is pending

#### Scenario: A source with a unit for its newest output is not pending

- **GIVEN** a source whose newest conversion output has a contextualization work-unit in any status
- **WHEN** the pending set is evaluated
- **THEN** the source is not pending

#### Scenario: A re-converted source is pending again

- **GIVEN** a source with a work-unit for an earlier conversion output and a newer output with none
- **WHEN** the pending set is evaluated
- **THEN** the source is pending

#### Scenario: A source without a conversion output is not pending

- **GIVEN** a source that has never produced a conversion output
- **WHEN** the pending set is evaluated
- **THEN** the source is not pending
