# Delta for Conversion UI

## MODIFIED Requirements

### Requirement: Display job monitoring table

The system SHALL render a job table at the `/ui/jobs` path showing all conversion jobs with their key attributes.
The title column SHALL display the enriched `Source.title` whenever it is non-`NULL`, falling back to `ConversionJob.title` (the submit-time placeholder — KaraKeep id, or `aizk_uuid` string) only when `Source.title` is `NULL`. (Previously: the requirement listed `title` as a column without specifying its source; the implementation rendered `ConversionJob.title or Source.title`, which always resolved to the placeholder because `ConversionJob.title` is non-null.)

#### Scenario: Job table renders on page load

- **GIVEN** conversion jobs exist in the system
- **WHEN** an operator navigates to the jobs UI
- **THEN** a table is displayed with columns for job identifier, internal bookmark identifier, KaraKeep identifier, title, status, attempt count, queued time, started time, finished time, and error code

#### Scenario: Title column shows enriched Source title when available

- **GIVEN** a job whose `Source.title = "Attention Is All You Need"` and whose `ConversionJob.title` is a placeholder UUID
- **WHEN** an operator loads the jobs page
- **THEN** the title column for that row displays `"Attention Is All You Need"`, not the placeholder

#### Scenario: Title column falls back to placeholder when no enriched title exists

- **GIVEN** a job whose `Source.title` is `NULL`
- **WHEN** an operator loads the jobs page
- **THEN** the title column displays the `ConversionJob.title` placeholder (KaraKeep id when present, otherwise `aizk_uuid`)

#### Scenario: Page loads within acceptable time for large job lists

- **GIVEN** up to 1000 jobs exist in the system
- **WHEN** the operator loads the jobs page
- **THEN** the page renders within 2 seconds

### Requirement: Filter and search jobs across the full job set

The system SHALL provide status and text filters that operate across all jobs in the system, not only those visible on the current page.
The text search SHALL match against the enriched `Source.title` in addition to job-level fields (`ConversionJob.title`, `Source.karakeep_id`, `aizk_uuid`, job id), so that searching by document title finds jobs whose only meaningful title lives on the Source row rather than the placeholder on the job row. (Previously: the text search clause read only `ConversionJob.title`, so searches by document title returned no matches even after enrichment populated `Source.title`.)

#### Scenario: Search by enriched document title

- **GIVEN** a job whose `Source.title = "Attention Is All You Need"` and whose `ConversionJob.title` is a placeholder UUID
- **WHEN** an operator searches for `"attention"`
- **THEN** that job appears in the filtered results

#### Scenario: Search by KaraKeep id still works

- **GIVEN** a job sourced from a KaraKeep bookmark
- **WHEN** an operator searches for the bookmark's KaraKeep id
- **THEN** that job appears in the filtered results

#### Scenario: Search term matches no jobs

- **GIVEN** no job's enriched title, internal identifier, KaraKeep identifier, or job-level title matches the operator's search term
- **WHEN** the operator submits the search
- **THEN** the table renders an empty result state rather than a stale or unfiltered list
