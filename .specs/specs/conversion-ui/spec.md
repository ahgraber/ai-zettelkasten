# Conversion UI Specification

> Translated from Spec Kit on 2026-03-21
> Source: specs/001-docling-conversion-service/spec.md

## Purpose

The Conversion UI provides an operator-facing web interface for monitoring conversion job status, filtering and searching jobs, and triggering retry or cancel actions.
It is an HTMX-powered single-page interface served alongside the conversion API.

## Requirements

### Requirement: Display job monitoring table

The system SHALL render a job table at the `/ui/jobs` path showing all conversion jobs with their key attributes.

#### Scenario: Job table renders on page load

- **GIVEN** conversion jobs exist in the system
- **WHEN** an operator navigates to the jobs UI
- **THEN** a table is displayed with columns for job identifier, internal bookmark identifier, KaraKeep identifier, title, status, attempt count, queued time, started time, finished time, and error code

#### Scenario: Page loads within acceptable time for large job lists

- **GIVEN** up to 1000 jobs exist in the system
- **WHEN** the operator loads the jobs page
- **THEN** the page renders within 2 seconds

### Requirement: Filter and search jobs server-side

The system SHALL provide server-side status and text filters that operate across all jobs, not just the current page.

#### Scenario: Filter by status

- **GIVEN** jobs exist with multiple statuses
- **WHEN** an operator selects a status filter
- **THEN** the table updates to show only jobs matching the selected status

#### Scenario: Text search across identifiers and title

- **GIVEN** jobs exist with various identifiers and titles
- **WHEN** an operator enters a text search term
- **THEN** the table updates to show only jobs whose internal identifier, KaraKeep identifier, or title matches the search term

### Requirement: Retry and cancel jobs via bulk actions

The system SHALL provide multi-select checkboxes and Retry and Cancel action buttons that apply to all selected jobs and display a result summary.

#### Scenario: Retry selected failed jobs

- **GIVEN** an operator selects one or more failed jobs
- **WHEN** the operator clicks Retry
- **THEN** the selected jobs are reset to queued status and a confirmation summary is displayed

#### Scenario: Cancel selected running jobs

- **GIVEN** an operator selects one or more running or queued jobs
- **WHEN** the operator clicks Cancel
- **THEN** the system attempts cancellation on all selected jobs and displays a result summary

#### Scenario: Bulk action confirmed within acceptable time

- **GIVEN** an operator submits a retry or cancel bulk action
- **WHEN** the action completes
- **THEN** the result is displayed within 5 seconds of the action being submitted

## Technical Notes

- **Implementation**: `aizk/conversion/ui/`
- **Dependencies**: conversion-api (bulk action endpoints at `/v1/jobs/actions`)
- **Rendering**: HTMX-powered; server-side filtering and sorting; no client-side JavaScript frameworks
