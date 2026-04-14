# Proposal: conversion-ui scenario coverage

## Intent

The 2026-04-14 SDD-compliance review of the conversion-ui baseline surfaced two contract gaps and tightened the spec with new scenarios:

1. **Empty filter/search state is indistinguishable from "no jobs yet."**
   The UI template renders the same "No jobs yet.
   Submit a bookmark to get started." message whether no jobs exist at all or the operator's filters simply match nothing.
2. **Bulk-action result summary does not distinguish ineligible from errored.**
   The UI route lumps ineligible-state transitions (e.g., cancelling a SUCCEEDED job) together with unexpected exceptions in a single "failed" counter, so an operator cannot tell whether a job was legitimately skipped or the system misbehaved.

Baseline `conversion-ui/spec.md` now carries scenarios for both cases (`Search term matches no jobs` and `Bulk action with mixed eligibility`).
This change brings the implementation and tests into alignment with that tightened contract.

> **Note on option C.** The new scenarios were added directly to baseline during the review rather than flowing through a delta first. This change's delta is documentary: it records the scenarios as the contract additions being implemented here. `sdd-sync` is a no-op for this change because baseline already reflects the end state.

## Scope

**In scope:**

- UI handler (`aizk.conversion.api.routes.ui.ui_job_actions`): split the current `success` / `errors` counters into `applied` / `ineligible` / `errored`, classifying `ValueError` from per-action helpers and missing-job lookups as `ineligible`.
- Notice template string: render the three-way breakdown when any category is non-zero.
- Empty-state template branch in `jobs_panel.html`: distinguish filtered-empty (`status_filter` or `search` is set) from system-empty (no jobs exist at all).
- Tests covering both scenarios.

**Out of scope:**

- `POST /v1/jobs/actions` (`BulkActionResponse`) in the conversion-api — that endpoint already returns per-job results and is not consumed by the UI handler.
- Error-classification changes to `_apply_job_retry` / `_apply_job_cancel` / `_apply_job_delete` — they already raise `ValueError` on ineligible state transitions; this change reads that signal, it does not change it.
- Visual redesign of the job table, filter UI, or notice styling beyond the text content.
- Observability changes (metrics, structured logs).

## Approach

`ui_job_actions` currently counts `success` and `errors` across `selected_ids`, swallowing both `ValueError` (ineligible transition) and job-not-found (`None` from `session.get`) into `errors`.
The split is straightforward: add an `ineligible` counter, increment it on `ValueError` and on missing-job lookups, and keep `errored` for any residual unexpected exceptions (currently none are caught — a `BaseException` fallback is out of scope).

The notice string moves from a single fixed template to a joined list of non-zero categories, so a clean "all applied" case still reads as one sentence.

For the empty-state, `_load_jobs_page` already returns `filtered_total` alongside `total_jobs`.
The template only needs a conditional: if `page.search or page.status_filter` is set, render a filtered-empty message; otherwise keep the current system-empty message.

## Schema Impact

None.
Change is confined to the UI handler and templates; no OpenAPI operation signatures, request bodies, or response models change.
