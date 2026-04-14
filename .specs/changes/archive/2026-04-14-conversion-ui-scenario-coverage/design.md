# Design: conversion-ui scenario coverage

## Context

The UI has its own bulk-action handler at `POST /ui/jobs/actions` (`aizk.conversion.api.routes.ui.ui_job_actions`); it does **not** call the API's `POST /v1/jobs/actions` endpoint.
The UI handler directly manipulates jobs via the per-action helpers (`_apply_job_retry`, `_apply_job_cancel`, `_apply_job_delete`), each of which raises `ValueError` when the job's current state does not permit the requested transition.

`_load_jobs_page` already returns both `total_jobs` (count of all jobs) and `filtered_total` (count after filters).
The empty-state template branch has the data it needs; it currently just doesn't use it.

## Decisions

### Decision: Classify `ValueError` and missing-job lookups together as "ineligible"

The UI handler encounters two non-success outcomes per job id:

1. `session.get(ConversionJob, job_id)` returns `None` — the id does not exist.
2. `_apply_job_*` raises `ValueError` — the job exists but its state does not permit the action.

Both map to a single `ineligible` counter.
From an operator perspective, the remedy is the same (refresh the page; do not expect the system to retry).
Distinguishing them in the UI summary would add diagnostic granularity for a rare case (a job deleted between page render and submit).
If this turns out to be a recurring signal in operator practice, splitting can be revisited.

**Alternatives considered:**

- Three-bucket `applied` / `ineligible` / `not_found` — stronger diagnostic power; rejected as premature given the rarity of the not-found path in normal operation.
- Keep missing as `errored` — masks the UI-staleness signal and equates it with unexpected exceptions, which is a worse diagnostic than lumping with `ineligible`.

### Decision: Notice string as a joined list of non-zero categories

Current notice: `"{success} jobs {action_label}; {errors} failed."`

New notice shape: join only non-zero categories with "; ".
Examples:

- All applied, none skipped: `"3 jobs retried."`
- Mixed: `"2 jobs cancelled; 1 skipped as ineligible."`
- All ineligible: `"0 jobs cancelled; 3 skipped as ineligible."` — the zero `applied` stays visible so the operator knows nothing happened.
- Selection empty: unchanged — `"Select at least one job."`

`errored` is reserved for a future category if we start catching `Exception` beyond `ValueError`; for this change it stays at zero and the template omits it.
No new exception catches are introduced.

### Decision: Empty-state branch predicate = `page.search or page.status_filter`

In `jobs_panel.html`, the existing `{% if page.jobs %}...{% else %}...{% endif %}` branch becomes a three-way:

- `page.jobs` non-empty → render rows.
- `page.jobs` empty AND (`page.search` or `page.status_filter`) → "No jobs match your filters.
  Try clearing the search or status filter."
- `page.jobs` empty AND neither filter set → preserve current "No jobs yet.
  Submit a bookmark to get started."

`filtered_total` is not used as the predicate because it is always zero in both empty branches; the filter state is the correct signal.

## Architecture

```text
POST /ui/jobs/actions (ui_job_actions)
  │
  ├─ validate action ∈ {retry, cancel, delete}
  ├─ for job_id in selected_ids:
  │    ├─ job = session.get(ConversionJob, job_id)
  │    ├─ if job is None: ineligible += 1; continue
  │    └─ try _apply_job_<action>(job, now)
  │       ├─ success → applied += 1
  │       └─ ValueError → ineligible += 1
  ├─ session.commit()
  ├─ notice = format(applied, ineligible, action_label, selected_ids)
  └─ render jobs_panel.html with {page: _load_jobs_page(..., notice)}


GET/POST /ui/jobs → jobs_panel.html (or jobs.html on non-HX request)
  │
  └─ tbody branch:
       jobs non-empty        → rows
       empty AND filtered    → "No jobs match your filters..."
       empty AND unfiltered  → "No jobs yet. Submit a bookmark..."
```

## Risks

- **Translation drift in notice wording.**
  The notice string is currently a plain f-string, not translated.
  This change keeps it that way; any future i18n work will need to revisit all notice branches together.
- **Test fragility on notice text.**
  Tests that assert on the full notice string will need updating.
  The refactor compresses the per-category formatting into a small helper so the assertion surface is narrower.
