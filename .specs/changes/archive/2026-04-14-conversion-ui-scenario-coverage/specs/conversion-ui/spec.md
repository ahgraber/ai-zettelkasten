# Delta for conversion-ui

> **Documentary delta.** Baseline `conversion-ui/spec.md` already carries the scenarios introduced here — they landed during the 2026-04-14 SDD-compliance review. This delta records the contract additions being implemented by this change; `sdd-sync` will be a no-op.

## MODIFIED Requirements

### Requirement: Filter and search jobs across the full job set

The system SHALL provide status and text filters that operate across all jobs in the system, not only those visible on the current page.
Added scenarios exercising filtered-empty boundary behavior.

#### Scenario: Search term matches no jobs

- **GIVEN** no job's internal identifier, KaraKeep identifier, or title matches the operator's search term
- **WHEN** the operator submits the search
- **THEN** the table renders an empty result state rather than a stale or unfiltered list

### Requirement: Retry and cancel jobs via bulk actions

The system SHALL allow an operator to select multiple jobs from the job table and apply a Retry or Cancel action to all selected jobs, and SHALL display a summary identifying which jobs the action was applied to and which were skipped.
Added scenario for mixed-eligibility selections, strengthening the summary contract.

#### Scenario: Bulk action with mixed eligibility

- **GIVEN** an operator has selected a set of jobs in which some are eligible for the chosen action and some are not
- **WHEN** the operator submits the bulk action
- **THEN** the result summary distinguishes jobs that the action was applied to from jobs that were skipped as ineligible, and no ineligible job's status is altered
