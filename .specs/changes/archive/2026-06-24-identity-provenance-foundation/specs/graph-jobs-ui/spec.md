# Delta for graph-jobs-ui

## MODIFIED Requirements

### Requirement: Display the contextualization jobs table

The system SHALL render a jobs page listing all contextualization work-units, each showing its job identifier, status, attempt count, queued/started/finished times, and error code.
For each job it SHALL display a human-readable document title — the enriched `Source.title` for the job's source when that is non-`NULL`, falling back to the durable source identity (`source_id`) when it is not.

Serves: coherent-pipeline-foundation

> Previously: the durable source identity was named `aizk_uuid`.

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
- **THEN** the title for that row displays the source `source_id`

### Requirement: Filter and search jobs across the full job set

The system SHALL provide a status filter and a text search that operate across all contextualization jobs, not only those on the current page.
The text search SHALL match the job identifier, the source `source_id`, the source title, and the `conversion_output` identifier.

Serves: coherent-pipeline-foundation

> Previously: the durable source identity was named `aizk_uuid`.

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
