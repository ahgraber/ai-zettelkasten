# Tasks: conversion-ui scenario coverage

## 1. Handler: split bulk-action counters

- [x] In `src/aizk/conversion/api/routes/ui.py`, replace the `success` / `errors` counters in `ui_job_actions` with `applied` / `ineligible` counters (the third `errored` bucket is deferred — see `design.md`).
- [x] Route the "job not found" branch (`session.get(...)` returns `None`) to `ineligible`.
- [x] Route the `ValueError` caught from `_apply_job_retry` / `_apply_job_cancel` / `_apply_job_delete` to `ineligible`.
- [x] Extract a small `_format_bulk_notice(applied, ineligible, action_label, selected_ids)` helper that returns the notice string.
  Return "Select at least one job." when `selected_ids` is empty (preserve current wording).
  Otherwise join only non-zero categories with "; " and end with ".".

## 2. Template: empty-state branch

- [x] In `src/aizk/conversion/templates/jobs_panel.html`, replace the existing single `{% else %}` branch of the `{% for job in page.jobs %}` loop with: - filtered-empty message when `page.search or page.status_filter` is truthy - system-empty message otherwise (preserve existing wording "No jobs yet.
  Submit a bookmark to get started.")
- [x] Use an `aria-live="polite"` container or inherit the existing one so the message is announced to assistive tech on HTMX swap.

## 3. Tests: filter scenarios

- [x] Add a test that seeds zero jobs, calls `GET /ui/jobs` with no filter, and asserts the response body contains the system-empty message.
- [x] Add a test that seeds one or more jobs and calls `GET /ui/jobs?search=<term-that-matches-none>`, asserting the response contains the filtered-empty message and does NOT contain the system-empty message.
- [x] Add a test that seeds jobs in multiple statuses and calls `GET /ui/jobs?status=<status-with-no-matches>`, asserting the same filtered-empty behavior.

## 4. Tests: bulk-action scenarios

- [x] Add a test that selects a mix of one eligible-for-cancel job (QUEUED) and one ineligible-for-cancel job (SUCCEEDED), submits `POST /ui/jobs/actions` with `action=cancel`, and asserts:
  \- the eligible job's status transitions to CANCELLED
  \- the ineligible job's status is unchanged
  \- the rendered notice contains both "1 jobs cancelled" and "1 skipped as ineligible"
- [x] Add a test that selects a non-existent job id alongside an eligible job and asserts it is counted as ineligible (not a 500, not a separate category).
- [x] Add a test for an all-eligible selection asserting the notice has no "skipped as ineligible" phrase.
- [x] Update any existing tests that asserted on the old notice string (`"N jobs X; M failed."`) to match the new format.

## 5. Verification

- [x] Run `uv run pytest tests/` and confirm all tests pass.
  Verified: `tests/conversion/integration/test_ui_jobs.py` — 10/10 pass.
  Other suite-wide failures (`test_hashing`, `test_startup`, `test_conversion_flow`, `test_health_api`, `test_whitespace_normalization`) pre-exist on this branch and are unrelated to this change.
- [ ] Manually exercise the UI: load `/ui/jobs` with an empty database, with filters that match nothing, and with a mixed-eligibility bulk action; confirm each message matches spec scenarios.
