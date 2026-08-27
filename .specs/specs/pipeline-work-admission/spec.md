# Pipeline Work Admission Specification

> Synced from change `pipeline-work-admission` on 2026-08-27

## Purpose

The pipeline-work-admission capability defines how work-units come to exist at a stage.
A stage may declare a **pending-work derivation**: given current upstream artifact and work-unit state, the set of work that should exist at the stage but has no work-unit.
An **admission pass** evaluates that derivation and creates the result through the stage's own enqueue primitive, so a pass produces units indistinguishable from any other path's and repeated passes are safe with no new dedupe mechanism.

Declaring a derivation is optional and feature-detected: a stage that declares none is fully conformant and is simply never admitted into.
Admission is distinct from the discovery in `pipeline-stage-runtime`, which selects already-queued units to claim; admission creates units, discovery consumes them.

Because a derivation reads state rather than events, it is self-healing — work that failed to be created is created on the next pass — and it carries no memory, so what one pass leaves unadmitted stays pending for the next.
Two controls bound the cost: a stage may declare a **capacity limit** over its own actionable backlog, binding on every path that creates work for it, and automatic admission is off for a stage until explicitly enabled.

## Requirements

### Requirement: A stage's pending work is declarable and queryable

A stage MAY declare a pending-work derivation: a definition, over upstream artifact and work-unit state, of the work that should exist at the stage but has no work-unit.
Whether a stage declares one SHALL be queryable.
A stage that declares none SHALL remain fully operable, and no admission SHALL create work-units for it.

#### Scenario: A stage without a derivation is never admitted into

- **GIVEN** a registered stage that declares no pending-work derivation
- **WHEN** admission runs across stages
- **THEN** no work-unit is created for that stage, and its existing enqueue and processing behavior is unchanged

#### Scenario: The declaration is feature-detected

- **GIVEN** one stage with a declared derivation and one without
- **WHEN** each stage is queried for the capability
- **THEN** the declaring stage reports its derivation and the other reports none

### Requirement: Pending work is derived from current state alone

For any stage declaring a pending-work derivation, the pending set SHALL be a function of current upstream artifact and work-unit state — not of any record of prior admission activity.
Identical state SHALL yield an identical pending set, and work that exists in the pending set but is not admitted by one evaluation SHALL remain in the pending set for the next.

#### Scenario: Unadmitted work stays pending

- **GIVEN** pending work an admission evaluation did not admit
- **WHEN** the pending set is evaluated again with no state change
- **THEN** that work is still pending

#### Scenario: The derivation has no memory

- **GIVEN** two evaluations of a stage's pending set against identical upstream and work-unit state
- **WHEN** the results are compared
- **THEN** they are the same set

### Requirement: Admission creates exactly the pending set through the stage's own enqueue

An admission pass SHALL create work-units only for work in the stage's pending set, and each admitted unit SHALL be identical — in identity, content, and downstream processing — to the unit any other enqueue path would create for the same work.
Re-running admission over unchanged state SHALL create no new work-unit.

#### Scenario: A repeated pass admits nothing new

- **GIVEN** a completed admission pass and no upstream change since
- **WHEN** admission runs again
- **THEN** no new work-unit exists

#### Scenario: An admitted unit equals a manually enqueued unit

- **GIVEN** the same pending work
- **WHEN** it is admitted by a pass in one case and enqueued through another path in the other
- **THEN** the resulting work-units are identical and downstream processing cannot distinguish them

#### Scenario: Work outside the pending set is untouched

- **GIVEN** sources that are not pending at the stage
- **WHEN** an admission pass runs
- **THEN** no work-unit is created for them

### Requirement: Automatic admission is governed by per-stage enablement

Automatic admission SHALL create no work-units for a stage unless it is explicitly enabled for that stage; enabling one stage SHALL NOT enable another.
While enabled for a stage, work entering the stage's pending set SHALL be admitted without operator action.

#### Scenario: Disabled admission admits nothing

- **GIVEN** a stage with pending work and automatic admission not enabled for it
- **WHEN** the system runs
- **THEN** no work-unit is created for that stage

#### Scenario: Enablement is per stage

- **GIVEN** automatic admission enabled for one stage and not another, both with pending work
- **WHEN** admission runs
- **THEN** the enabled stage's pending work is admitted and the other stage's is not

#### Scenario: Enabled admission needs no operator

- **GIVEN** automatic admission enabled for a stage
- **WHEN** new work enters the stage's pending set
- **THEN** a work-unit for it is created without any operator action

### Requirement: A declared capacity limit bounds work-unit creation on every path

A stage MAY declare a capacity limit over its actionable backlog — its work-units queued or awaiting retry.
Every path that creates work for the stage SHALL evaluate the limit and refuse creation when the backlog is at or above it; a request that resolves to an existing work-unit SHALL return that unit rather than be refused, since reuse adds nothing to the backlog.
The evaluation and the creation SHALL occur within one transaction, so that a backend providing serializable writes admits no unit beyond the limit.
An admission pass SHALL NOT admit beyond capacity; work left unadmitted remains pending.
A stage declaring no limit SHALL accept new work without capacity refusal.

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

### Requirement: Reported pending work matches what admission would admit

For any stage declaring a pending-work derivation, the pending work reported to observers SHALL be the same set an unconstrained admission pass would admit at that moment.

#### Scenario: The report and the pass agree

- **GIVEN** a stage's reported pending set
- **WHEN** an admission pass runs immediately after with no capacity constraint and no state change
- **THEN** the admitted work is exactly the reported set

## Technical Notes

- **Implementation**: `src/aizk/graph/admission.py` — the per-stage adapter (`AdmissionAdapter`, `admission_adapter_for`), the pass (`run_admission_pass`), and the in-worker loop (`AdmissionLoop`); `src/aizk/graph/capacity.py` — the actionable-backlog count, the refusal (`StageAtCapacityError`), and the per-batch headroom bulk callers read once.
- **Declaring stages**: contextualization derives over conversion outputs (`aizk.graph.enqueue`), mention-extraction over active chunking runs (`aizk.graph.extraction_workunit`).
- **Capacity seam**: the check sits inside the two work-unit construction sites, after the idempotency-dedupe branch, so intake routes, admission passes, backfill commands, and notebooks are all subject to it with no bypass.
  Each caller states its limit explicitly; `0` declares no limit.
- **Configuration**: `AdmissionConfig` on the graph config, per the `AIZK_<SECTION>__<FIELD>` convention — per-stage enable flags (default off), a shared loop interval, per-stage queue depths, and the retry-after seconds shared with the conversion service.
- **Design record**: the in-worker loop, the enqueue-seam capacity placement, the per-stage adapter, and the configuration surface are decided in the change `pipeline-work-admission` design.
