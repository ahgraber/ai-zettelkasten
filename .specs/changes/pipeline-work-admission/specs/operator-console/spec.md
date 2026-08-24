# Delta for Operator Console

## MODIFIED Requirements

### Requirement: Dashboard summarizes per-stage health

> Previously: the dashboard summarized only existing work-units; work not yet admitted to a stage was invisible.

The console SHALL provide a dashboard listing, for every registered stage, its work-unit counts grouped by the generic work-unit lifecycle vocabulary.
Each stage-native status SHALL be counted under exactly one generic category — no unit dropped, none double-counted.
Within the failed category, the dashboard SHALL distinguish units awaiting retry from permanently failed units, so repairability is visible at a glance.
For any registered stage that declares a pending-work derivation, the dashboard SHALL also show the stage's count of pending sources.
The pending count SHALL sit outside the lifecycle rollup — it counts no work-unit, and the per-stage unit total SHALL continue to equal the number of the stage's work-units.
A stage that declares no derivation SHALL show no pending count.

Serves: visible-stage-coverage

#### Scenario: Graph stage counts appear under their own lifecycle statuses

- **GIVEN** contextualization units exist across queued, running, and failed statuses
- **WHEN** an operator loads the dashboard
- **THEN** the contextualization row shows the count of units in each lifecycle category

#### Scenario: Conversion native statuses roll up without loss

- **GIVEN** conversion jobs exist in native statuses including `UPLOAD_PENDING`, `FAILED_RETRYABLE`, and `FAILED_PERM`
- **WHEN** an operator loads the dashboard
- **THEN** every job is counted under exactly one generic category, and the per-stage total equals the number of conversion jobs

#### Scenario: Conversion failed counts distinguish repairability

- **GIVEN** conversion jobs exist in both `FAILED_RETRYABLE` and `FAILED_PERM`
- **WHEN** an operator loads the dashboard
- **THEN** the conversion failed count distinguishes the awaiting-retry jobs from the permanently failed ones

#### Scenario: Graph-stage failed counts distinguish repairability

- **GIVEN** contextualization units exist in `FAILED` both with and without a pending retry wait
- **WHEN** an operator loads the dashboard
- **THEN** the stage's failed count distinguishes the awaiting-retry units from the permanently failed ones

#### Scenario: A declaring stage shows its pending count

- **GIVEN** a stage with a pending-work derivation and sources pending admission
- **WHEN** an operator loads the dashboard
- **THEN** the stage's row shows the count of pending sources

#### Scenario: The pending count does not perturb the unit rollup

- **GIVEN** a stage with both existing work-units and pending sources
- **WHEN** an operator loads the dashboard
- **THEN** the stage's per-status counts and unit total are unchanged by the pending sources, and the pending count appears separately

#### Scenario: A stage without a derivation shows no pending count

- **GIVEN** a registered stage that declares no pending-work derivation
- **WHEN** an operator loads the dashboard
- **THEN** the stage's row shows no pending figure

## ADDED Requirements

### Requirement: A declaring stage's pending sources are listed in its monitor

For any registered stage that declares a pending-work derivation, the console SHALL provide a listing of the stage's pending sources, each identified by its source identity and by the same human-readable title contract the task monitor applies to work-units.
The listed set SHALL match the pending work the stage's derivation reports.

Serves: visible-stage-coverage

#### Scenario: Pending sources are listed for a declaring stage

- **GIVEN** a stage with sources pending admission
- **WHEN** an operator opens the stage's pending listing
- **THEN** each pending source appears with its source identity and title

#### Scenario: The listing agrees with the dashboard count

- **GIVEN** a stage whose dashboard row shows a pending count
- **WHEN** an operator opens the stage's pending listing with no intervening state change
- **THEN** the number of listed sources equals the dashboard count

#### Scenario: A stage without a derivation has no pending listing

- **GIVEN** a registered stage that declares no pending-work derivation
- **WHEN** an operator views that stage in the console
- **THEN** no pending listing is offered for it

### Requirement: A declaring stage's stale sources are visible and actionable

For any registered stage that declares a staleness derivation, the dashboard SHALL show the stage's count of stale sources, and the stage's monitor SHALL identify its stale work-units so an operator can select them — individually or in bulk — for the stage's declared actions.
The identified set SHALL match the stage's staleness derivation.
A stage that declares no staleness derivation SHALL show no stale figure and no stale marking.

Serves: visible-stage-coverage

#### Scenario: The stale count appears for a declaring stage

- **GIVEN** a stage with a staleness derivation and stale sources
- **WHEN** an operator loads the dashboard
- **THEN** the stage's row shows the count of stale sources

#### Scenario: Stale units are selectable in the monitor

- **GIVEN** a stage's monitor showing both stale and current work-units
- **WHEN** an operator views the listing
- **THEN** the stale units are identifiable and can be selected together for a declared action

#### Scenario: A stage without a staleness derivation shows none

- **GIVEN** a registered stage that declares no staleness derivation
- **WHEN** an operator views the dashboard and the stage's monitor
- **THEN** no stale count or stale marking appears for it
