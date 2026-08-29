# Delta for pipeline-work-admission

## ADDED Requirements

### Requirement: An admission pass is bounded independent of the capacity limit

An admission pass SHALL admit at most a fixed per-pass batch, whether or not the stage declares a capacity limit.
Work beyond the batch remains pending and is admitted by later passes, so an unlimited stage still drains fully over repeated passes while no single pass grows with the pending corpus.

Serves: trustworthy-operator-view

#### Scenario: An unlimited stage admits a bounded batch per pass

- **GIVEN** a stage with no declared capacity limit and pending work exceeding the per-pass batch
- **WHEN** an admission pass runs
- **THEN** it admits at most the batch, and the remainder is still pending

#### Scenario: A limited stage is bounded by the smaller of batch and headroom

- **GIVEN** a stage whose remaining capacity exceeds the per-pass batch
- **WHEN** an admission pass runs
- **THEN** it admits at most the batch

#### Scenario: Repeated passes drain the pending set

- **GIVEN** pending work left unadmitted by a bounded pass and no capacity constraint
- **WHEN** admission passes repeat with no new work arriving
- **THEN** the pending set is eventually empty

### Requirement: A bounded pending evaluation does bounded work

A pending-work derivation invoked with a limit SHALL return the first pending work in the derivation's order, and the cost of the evaluation SHALL be bounded by the limit and the pending set — it SHALL NOT grow with the number of sources already admitted.

Serves: trustworthy-operator-view

#### Scenario: Admitted corpus growth does not slow a bounded evaluation

- **GIVEN** two corpora with the same pending work, one holding many more already-admitted sources
- **WHEN** the pending set is evaluated with the same limit against each
- **THEN** both evaluations return the same pending work at a cost that does not scale with the admitted corpus

## MODIFIED Requirements

### Requirement: A declared capacity limit bounds work-unit creation on every path

> Previously: the requirement did not state whether operator status transitions were subject to the limit, and did not state what the limit is not — leaving room to read it as a spend bound.

A stage MAY declare a capacity limit over its actionable backlog — its work-units queued or awaiting retry.
The limit is a creation gate measured against the instantaneous actionable backlog at creation time; it is not an invariant ceiling on the backlog (the exempt transitions below may exceed it) and not a bound on cumulative work or external spend.
Every path that creates work for the stage SHALL evaluate the limit and refuse creation when the backlog is at or above it; a request that resolves to an existing work-unit SHALL return that unit rather than be refused, since reuse adds nothing to the backlog.
A status transition that returns an existing work-unit to the actionable backlog — an operator retry or re-admission — is exempt from the limit: it creates no unit, and refusing it would block remediation of work already admitted.
The evaluation and the creation SHALL occur within one transaction, so that a backend providing serializable writes admits no unit beyond the limit.
An admission pass SHALL NOT admit beyond capacity; work left unadmitted remains pending.
A stage declaring no limit SHALL accept new work without capacity refusal.

Serves: trustworthy-operator-view

#### Scenario: New work is refused at capacity

- **GIVEN** a stage whose actionable backlog is at its declared capacity
- **WHEN** any path attempts to create a new work-unit
- **THEN** the creation is refused and no unit is added

#### Scenario: A duplicate bypasses the capacity check

- **GIVEN** a stage at capacity and a request resolving to an already-existing work-unit
- **WHEN** the request is made
- **THEN** the existing unit is returned and nothing is refused

#### Scenario: Admission stops at capacity and resumes after drain

- **GIVEN** more pending work than remaining capacity
- **WHEN** an admission pass runs and later the backlog drains below the limit
- **THEN** the pass admits only up to capacity, and a later pass admits from the still-pending remainder

#### Scenario: No declared limit means no capacity refusal

- **GIVEN** a stage that declares no capacity limit
- **WHEN** new work is created for it
- **THEN** no capacity refusal occurs

#### Scenario: An operator transition succeeds at capacity

- **GIVEN** a stage whose actionable backlog is at its declared capacity and an existing work-unit eligible for retry or re-admission
- **WHEN** an operator applies the transition
- **THEN** the transition succeeds and no capacity refusal occurs
