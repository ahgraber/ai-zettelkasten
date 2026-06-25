# Conversion UI Specification

> Translated from Spec Kit on 2026-03-21
> Source: specs/001-docling-conversion-service/spec.md

## Purpose

The Conversion UI provides an operator-facing web interface for monitoring conversion job status, filtering and searching jobs, and triggering retry or cancel actions.
It is an HTMX-powered single-page interface served alongside the conversion API.

## Requirements

### Requirement: Display job monitoring table

The system SHALL render a job table at the `/ui/jobs` path showing all conversion jobs with their key attributes.
The title column SHALL display the enriched `Source.title` whenever it is non-`NULL`, falling back to `ConversionJob.title` (the submit-time placeholder — KaraKeep id, or `source_id` string) only when `Source.title` is `NULL`. (Previously: the requirement listed `title` as a column without specifying its source; the implementation rendered `ConversionJob.title or Source.title`, which always resolved to the placeholder because `ConversionJob.title` is non-null.)

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
- **THEN** the title column displays the `ConversionJob.title` placeholder (KaraKeep id when present, otherwise `source_id`)

#### Scenario: Page loads within acceptable time for large job lists

- **GIVEN** up to 1000 jobs exist in the system
- **WHEN** the operator loads the jobs page
- **THEN** the page renders within 2 seconds

### Requirement: Filter and search jobs across the full job set

The system SHALL provide status and text filters that operate across all jobs in the system, not only those visible on the current page.
The text search SHALL match against the enriched `Source.title` in addition to job-level fields (`ConversionJob.title`, `Source.karakeep_id`, `source_id`, job id), so that searching by document title finds jobs whose only meaningful title lives on the Source row rather than the placeholder on the job row. (Previously: the text search clause read only `ConversionJob.title`, so searches by document title returned no matches even after enrichment populated `Source.title`.)

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

### Requirement: Retry and cancel jobs via bulk actions

The system SHALL allow an operator to select multiple jobs from the job table and apply a Retry or Cancel action to all selected jobs, and SHALL display a summary identifying which jobs the action was applied to and which were skipped.

#### Scenario: Retry selected failed jobs

- **GIVEN** an operator has selected one or more failed jobs
- **WHEN** the operator submits the Retry bulk action
- **THEN** the selected jobs are reset to queued status and a confirmation summary is displayed

#### Scenario: Cancel selected running jobs

- **GIVEN** an operator has selected one or more running or queued jobs
- **WHEN** the operator submits the Cancel bulk action
- **THEN** the system attempts cancellation on all selected jobs and displays a result summary

#### Scenario: Bulk action with mixed eligibility

- **GIVEN** an operator has selected a set of jobs in which some are eligible for the chosen action and some are not
- **WHEN** the operator submits the bulk action
- **THEN** the result summary distinguishes jobs that the action was applied to from jobs that were skipped as ineligible, and no ineligible job's status is altered

#### Scenario: Bulk action confirmed within acceptable time

- **GIVEN** an operator submits a retry or cancel bulk action
- **WHEN** the action completes
- **THEN** the result is displayed within 5 seconds of the action being submitted

## Technical Notes

- **Implementation**: `aizk/conversion/ui/`
- **Dependencies**: conversion-api (bulk action endpoints at `/v1/jobs/actions`)
- **Rendering**: HTMX-powered; server-side filtering and sorting; no client-side JavaScript frameworks
