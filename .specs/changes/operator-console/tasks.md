# Tasks: operator-console

## Stage descriptor contract

- [x] Define the `StageDescriptor` dataclass and the module-level registry (key, label, principal-receiving list/count queries, columns template, native statuses, rollup map, searchable identifiers, declared actions with eligibility/apply callables, optional drill-down detail composer).
- [x] Implement the identity rollup for graph stages and the conversion rollup map (`NEW`/`QUEUED`→queued, `RUNNING`/`UPLOAD_PENDING`→running, `SUCCEEDED`→succeeded, `FAILED_RETRYABLE`/`FAILED_PERM`→failed, `CANCELLED`→cancelled).
- [x] Test: rollup exhaustiveness — every `ConversionJobStatus` and `WorkUnitStatus` member maps to exactly one generic category (fails on any future unmapped status).

## Conversion domain helper lift (independent of the descriptor group)

- [x] Move `_apply_job_retry`/`_apply_job_cancel`/`_apply_job_delete` and their eligibility sets from `aizk.conversion.api.routes.jobs` into a new `aizk.conversion.job_actions` domain module as public `apply_job_retry`/`apply_job_cancel`/`apply_job_delete` with docstrings; update the JSON API routes and the conversion HTML UI to call the lifted helpers.
- [x] Test: existing conversion job-action API and UI tests pass unchanged against the lifted helpers (behavior pinned before the console consumes them).

## Generic task monitor (graph stages first)

- [x] Add the generic console routes: `GET /ui/tasks` (stage-selected monitor), `GET /ui/tasks/{stage}/{id}` (drill-down), `POST /ui/tasks/{stage}/actions` (bulk retry/cancel, skip-and-report), all rendering from the registry.
- [x] Build the generic monitor template (filters, search, pagination, bulk-action form, flash summary) including the descriptor's columns partial; build the generic drill-down template (event trail + descriptor detail section).
- [x] Register the contextualization and extraction descriptors (list/count queries, columns partials, `_apply_retry`/`_apply_cancel` dispatch, runs-based drill-down composers).
- [x] Port the contextualization and extraction jobs-page integration tests to the unified paths: table rendering, whole-set status filter, title search, empty-result state, retry/cancel/mixed-eligibility bulk actions.
- [x] Test: title fallback for graph stages — `Source.title` when non-`NULL`, `source_id` otherwise.
- [x] Test: drill-down for a completed contextualization unit (three runs + succeeded trail) and for a failed-early unit (chunking shown, contextualization absent, failure event surfaced).
- [x] Test: drill-down for an extraction unit shows the extraction run with its lifecycle status plus the event trail.
- [x] Test: action equivalence — a console cancel and a direct `_apply_cancel` on equivalent contextualization units produce the same terminal status and equivalent durable event.
- [x] Test: perimeter on unified paths — a `Host` outside the allowlist is rejected on monitor, drill-down, and action routes; a served console route resolves the same principal as the JSON API (parameterized over registered stages).
- [x] Test: boundary validation — unknown stage key → 404, unknown unit → 404, undeclared action → 400, empty selection alters nothing with an informative summary, and a selection above the 100-id cap is rejected with no unit altered.
- [x] Test: seeded timing — a 1000-unit stage monitor page renders within 2 seconds; a bulk action over a realistic selection reports within 5 seconds.

## Dashboard

- [x] Add `GET /ui` rendering per-stage status counts from each descriptor's count query, rolled up to the generic lifecycle vocabulary; console root `/` redirects to `/ui`.
- [x] Test: graph-stage counts appear per lifecycle category, with failed counts split into awaiting-retry (`earliest_next_attempt_at` set) and permanent (`NULL`).
- [x] Test: registration seam — a test-double descriptor registered in a test app appears in the dashboard and the monitor, and its units list, filter, and drill down with no console route or template modification.
- [x] Test: declared capabilities — a test-double stage declaring no actions offers no action controls and its action route rejects all actions; one declaring no detail section renders the event trail alone in its drill-down.

## Conversion joins the console

- [x] Register the conversion descriptor: list/count queries over `ConversionJob`, native-status vocabulary, columns partial (bookmark/KaraKeep columns), searchable identifiers (`ConversionJob.title`, KaraKeep id), lifted `apply_job_retry`/`apply_job_cancel`/`apply_job_delete` dispatch (Retry, Cancel, Delete declared), `ConversionOutput` drill-down composer.
- [x] Test: conversion's declared Delete action removes selected terminal jobs and their `ConversionOutput`, skips an active job as ineligible with status unaltered, and reports both in the summary; graph stages reject Delete as an undeclared action.
- [x] Test: conversion monitor rows display native statuses (`UPLOAD_PENDING`, `FAILED_PERM`) and the conversion title fallback (submit-time placeholder when `Source.title` is `NULL`).
- [x] Test: search by KaraKeep id returns the job (stage-declared searchable identifiers exercised through the generic search path).
- [x] Test: stage-native eligibility — a Cancel selection including an `UPLOAD_PENDING` job skips it as ineligible with status unaltered while eligible jobs cancel.
- [x] Test: action equivalence — a console retry and a conversion JSON-API retry on equivalent jobs produce the same status, cleared fields, and equivalent durable requeue event.
- [x] Test: dashboard rollup lossless for conversion — every native status counted under exactly one generic category; per-stage total equals the job count; failed counts split `FAILED_RETRYABLE` from `FAILED_PERM`.
- [x] Test: principal-scoping parity — jobs seeded with a foreign `owner_id` are absent from the conversion monitor listing and dashboard counts, their drill-down responds not-found, and a bulk action including them reports them not-found without failing the batch.
- [x] Test: import boundary — the console's conversion descriptor imports only conversion domain code (`aizk.conversion.datamodel`, `aizk.conversion.queries`, `aizk.conversion.job_actions`); no route, wiring, or processing modules.

## Console shell and explorer path

- [x] Extract the shared shell partials (`_nav.html`, `_styles_base.html`, `_styles_jobs.html`) from the per-stage templates (in-flight on this branch).
- [x] Update the nav partial to the console sections (dashboard, task monitor, explorer) at the unified paths; retitle the app to the operator console; move the explorer to `/ui/explore/chunks`.
- [x] Port the navigation integration tests to the unified paths: every page links every section, current section marked active, HTMX panel fragments omit the nav.
- [x] Delete the superseded per-stage console routes and templates (`ui.py`/`extraction_ui.py` monitor routes, `jobs.html`, `extraction_jobs.html`) once the generic monitor covers them; update the explorer's old-path references.

## Conversion sheds HTML

- [x] Remove the conversion `ui` router, its templates, and template wiring from the conversion app; retarget the conversion root redirect to `/docs`.
- [x] Test: the conversion app serves no HTML — the former `/ui/jobs` path returns 404 and the app's OpenAPI schema contains no `/ui` paths.
- [x] Schema check: regenerate `conversion-api-openapi` and diff against `schemas/expected.md` (only the two `/ui` path removals).

## Completion sweeps

- [ ] Whole-tree sweep (`rg` across src, tests, templates, docs, notebooks, scripts) for retired paths (`/ui/graph/jobs`, `/ui/graph/extraction-jobs`, `/ui/graph/explorer`, conversion `/ui/jobs`) and stale references to the removed modules/templates.
- [ ] Refresh the graph and conversion package READMEs for the console/JSON-only split.
- [ ] Run the full suite via project tooling (`uv run pytest -n auto -m "not integration_lifecycle" tests/`; lifecycle marker separately) and the lint/format gates.
