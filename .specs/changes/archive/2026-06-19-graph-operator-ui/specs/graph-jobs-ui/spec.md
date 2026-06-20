# Delta for graph-jobs-ui

## ADDED Requirements

### Requirement: Graph UI routes enforce the graph API's perimeter

Every graph operator UI route — read views (jobs table, drill-down, explorer, search) and mutation actions (bulk retry/cancel) — SHALL enforce the same access perimeter as the graph JSON API: it SHALL resolve the request principal the API requires and SHALL be subject to the same trusted-host restriction.
A UI route SHALL NOT be reachable under conditions where the corresponding API route would be rejected.

#### Scenario: A UI route is rejected on a host the API would reject

- **GIVEN** a request whose `Host` is outside the trusted-host allowlist
- **WHEN** it targets a graph UI route
- **THEN** it is rejected by the same trusted-host restriction the graph API enforces, rather than being served

#### Scenario: UI routes require the same principal as the API

- **GIVEN** the graph API resolves a request principal on its routes
- **WHEN** a graph UI route is served
- **THEN** it resolves the same principal, so the UI is not a weaker perimeter than the API beside it

### Requirement: Display the contextualization jobs table

The system SHALL render a jobs page listing all contextualization work-units, each showing its job identifier, status, attempt count, queued/started/finished times, and error code.
For each job it SHALL display a human-readable document title — the enriched `Source.title` for the job's source when that is non-`NULL`, falling back to the durable source identity (`aizk_uuid`) when it is not.

#### Scenario: Jobs table renders on page load

- **GIVEN** contextualization work-units exist
- **WHEN** an operator navigates to the contextualization jobs page
- **THEN** a table is displayed with a row per job showing job identifier, status, attempt count, queued/started/finished times, and error code

#### Scenario: Title shows the enriched source title when available

- **GIVEN** a job whose source has `Source.title = "Attention Is All You Need"`
- **WHEN** an operator loads the jobs page
- **THEN** the title for that row displays `"Attention Is All You Need"`

#### Scenario: Title falls back to source identity when no enriched title exists

- **GIVEN** a job whose `Source.title` is `NULL`
- **WHEN** an operator loads the jobs page
- **THEN** the title for that row displays the source `aizk_uuid`

### Requirement: Filter and search jobs across the full job set

The system SHALL provide a status filter and a text search that operate across all contextualization jobs, not only those on the current page.
The text search SHALL match the job identifier, the source `aizk_uuid`, the source title, and the `conversion_output` identifier.

#### Scenario: Filter by status spans the whole job set

- **GIVEN** more jobs of a given status exist than fit on one page
- **WHEN** an operator filters by that status
- **THEN** the filtered results include matching jobs from beyond the current page and exclude jobs of other statuses

#### Scenario: Search by source title

- **GIVEN** a job whose source title is `"Attention Is All You Need"`
- **WHEN** an operator searches for `"attention"`
- **THEN** that job appears in the filtered results

#### Scenario: Search term matches no jobs

- **GIVEN** no job's identifier, source identity, source title, or conversion-output identifier matches the term
- **WHEN** the operator submits the search
- **THEN** the table renders an empty result state rather than a stale or unfiltered list

### Requirement: Retry and cancel jobs via bulk actions

The system SHALL allow an operator to select multiple jobs and apply a Retry or Cancel action to all selected, and SHALL display a summary distinguishing the jobs the action was applied to from those skipped as ineligible, altering no ineligible job's status.

#### Scenario: Retry selected failed jobs

- **GIVEN** an operator has selected one or more retryable failed jobs
- **WHEN** the operator submits the Retry bulk action
- **THEN** the selected jobs are returned to a queued state and a confirmation summary is displayed

#### Scenario: Cancel selected active jobs

- **GIVEN** an operator has selected one or more queued or running jobs
- **WHEN** the operator submits the Cancel bulk action
- **THEN** the system attempts cancellation on the selected jobs and displays a result summary

#### Scenario: Bulk action with mixed eligibility

- **GIVEN** a selection in which some jobs are eligible for the chosen action and some are not
- **WHEN** the operator submits the bulk action
- **THEN** the summary distinguishes applied jobs from those skipped as ineligible, and no ineligible job's status is altered

### Requirement: Show the per-job stage drill-down

For a selected job, the system SHALL show the graph-stage runs for the job's source — the chunking, document-summary, and chunk-contextualization runs, each with its lifecycle status (active or superseded) — together with the contextualization work-unit's lifecycle event trail for that job.
This lets an operator see which stages have produced a run for the source and where the work-unit's processing succeeded or failed.
The drill-down SHALL reflect the source's current run state: a stage that has not produced a run for the source SHALL be shown as absent rather than fabricated.

#### Scenario: A completed job shows all three stage runs and its event trail

- **GIVEN** a job whose source has completed chunking, summary, and contextualization
- **WHEN** an operator opens that job's drill-down
- **THEN** the chunking, document-summary, and chunk-contextualization runs are shown with their lifecycle status, alongside the work-unit's lifecycle event trail ending in a succeeded event

#### Scenario: A job that failed before contextualization shows the reached stages and the gap

- **GIVEN** a job whose source was chunked but whose contextualization did not complete
- **WHEN** an operator opens that job's drill-down
- **THEN** the chunking run is shown, the chunk-contextualization run is shown as absent, and the work-unit's failure event is surfaced from its event trail
