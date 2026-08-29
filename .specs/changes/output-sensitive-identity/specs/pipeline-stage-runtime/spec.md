# Delta for pipeline-stage-runtime

## MODIFIED Requirements

### Requirement: Every work-unit status change is recorded as a durable event in the same transaction

> Previously: only status changes were evented; a work-unit's creation wrote no durable event, so its trail began at first claim and the admitting path and principal were unrecoverable.

A work-unit's authoritative status SHALL be its single current-state value, and its creation and every subsequent status change SHALL be co-committed in the same transaction with an append-only event recording it.
A creation event SHALL record the path that created the unit and the acting principal.
A request that resolves to an existing work-unit SHALL NOT write a creation event, since it creates nothing.
Each event SHALL validate against a typed contract for its kind.
A committed creation or status change SHALL NOT exist without its event, and an event SHALL NOT exist without its creation or status change.

Serves: trustworthy-operator-view

#### Scenario: A status transition writes exactly one matching event

- **GIVEN** a work-unit in a given status
- **WHEN** its status transitions
- **THEN** the new status and one transition event recording it are durable, committed together

#### Scenario: A failed transition leaves neither the status change nor the event

- **GIVEN** a status transition whose transaction fails before commit
- **WHEN** the store is inspected
- **THEN** neither the status change nor a transition event for it is present

#### Scenario: Creation is evented with origin and principal

- **GIVEN** work with no existing work-unit, created through any path (intake, admission, backfill)
- **WHEN** the work-unit is created
- **THEN** the unit and one creation event recording the creating path and acting principal are durable, committed together

#### Scenario: Reuse writes no creation event

- **GIVEN** work that already has a work-unit
- **WHEN** a request for the same work resolves to the existing unit
- **THEN** no new creation event exists for that unit

#### Scenario: A failed creation leaves neither the unit nor the event

- **GIVEN** a work-unit creation whose transaction fails before commit
- **WHEN** the store is inspected
- **THEN** neither the work-unit nor a creation event for it is present
