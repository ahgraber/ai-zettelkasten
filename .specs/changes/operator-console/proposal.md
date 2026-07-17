# Proposal: operator-console

## Intent

Every pipeline stage today ships its own hand-copied operator UI: conversion serves one jobs page from its own app, and the graph app serves two more (contextualization, extraction) built by mirroring the first.
The operator juggles four pages across two origins with no navigation between them; the developer adding a stage copies ~200 lines of routes plus a full template set; and the extraction jobs page has no spec cover at all.
The task-monitoring surface is the same verb everywhere — list, filter, paginate, bulk retry/cancel, drill into events — so the repetition is measured, not speculative.
This change consolidates all operator HTML into one console app with a registry-driven task monitor, and reduces the conversion service to its JSON API.

## User Stories

### Story: single-pane-operations

As the owner-operator, I want one console — one origin, one navigation, health at a glance — covering every pipeline stage, so that I can monitor and repair my ingestion pipeline without juggling multiple apps and ports.

### Story: uniform-task-repair

As the owner-operator, I want to filter, inspect, retry, and cancel any stage's work-units through one consistent monitor, so that repairing a stuck pipeline uses the same moves regardless of which stage is stuck.

### Story: cheap-stage-onboarding

As the developer adding a pipeline stage, I want to register a stage descriptor instead of copying a route-and-template set, so that future stages (canonicalize, graph assembly) get an operator surface for the cost of an adapter.

All three ladder to the north star's self-hosting clause: the corpus is "self-hostable on minimal infrastructure so one person can own the entire system" — the console is the surface where that one person actually operates it.

## Scope

**In scope:**

- A new `operator-console` capability, served from the top-level `aizk.console` package mounted by the existing graph operator app (no new app or process): shared shell (navigation, styles, single origin), a dashboard landing page with per-stage work-unit status counts, a registry-driven task monitor covering all registered stages (conversion, contextualization, extraction), and a per-unit drill-down (lifecycle event trail plus per-stage runs/artifact detail).
- A stage-descriptor registry as the console's extension seam: each stage contributes its list query and columns, its native status vocabulary plus a rollup mapping to the generic work-unit lifecycle, its action-eligibility rules, and its action dispatch to existing domain helpers.
- Conversion joins the monitor as a registered stage; its retry/cancel/delete helpers are lifted out of the API route module into a domain module so the console imports domain code, not another app's routes.
  The delete action — previously offered only by conversion's HTML UI and never specified — is preserved as a conversion-declared console action and gains spec cover here.
- The conversion service sheds its HTML UI (routes and templates removed; its OpenAPI schema loses the `/ui` paths); it keeps all JSON API routes unchanged.
- New unified URL structure for the console (`/ui/tasks`, `/ui/tasks/{stage}/{id}`, `/ui/explore/...`); old per-stage paths are retired without redirects.
- Spec bookkeeping: `conversion-ui` is removed (absorbed), `graph-jobs-ui` is renamed and generalized into `operator-console`, the extraction jobs page gains spec cover, and conversion's delete action gains spec cover.
  The explorer's own spec is unchanged — its mounting inside the console shell is covered by the console's navigation and single-origin contracts.
- Absorbs the in-flight shared-shell work on this branch (`_nav.html`, `_styles_base.html`, `_styles_jobs.html`, navigation tests).

**Out of scope:**

- New explorer bodies (mentions, entities) — separate changes; only the shell they will mount into is built here.
- Any engine or lifecycle behavior: the console never schedules, claims, leases, or retries on its own clock — it is a read model plus thin command dispatch to existing domain helpers.
- Unifying status _storage_: `ConversionJobStatus` stays as persisted; the generic mapping is read-side only.
- Auth or perimeter changes: the console keeps the existing trusted-host allowlist and principal resolution.
- Job submission UI: submission remains a JSON API concern.
- Deployment topology beyond the app retitle: no new service, no reverse proxy, no port changes.
- Database schema changes: the console reads existing tables and projections; no migrations.
- Frontend framework changes: the console stays server-rendered Jinja + HTMX; adopting Datastar or SSE live-push is a separate preliminary-research ADR (`docs/decision-record/`) for when a surface has a measured real-time need.
- Metrics exporting (Prometheus/OpenTelemetry): the runner's `StageMetrics` seam already accepts a real backend; wiring an exporter is process-wide observability with its own ADR and change, not part of the UI read layer.

## Approach

Evolve, don't create: the graph operator app is already the de-facto console (light dependencies, shared config/auth/DB, hosts two of three stages), so it grows the third stage and is retitled, rather than standing up a new app.

Mechanism parking (formalized in `design.md`):

- **StageDescriptor** (the registry entry): stage key + label; list query and columns partial; native-status enum for display and action eligibility; native→`WorkUnitStatus` rollup for dashboard counts; the stage's declared actions with per-action eligibility, dispatching to `_apply_retry`/`_apply_cancel` (graph) and the lifted conversion helpers; drill-down composition — lifecycle events from `pipeline_events` (already stage-agnostic via `stage` + `work_unit_ref`) always, plus an optional per-stage artifact section (graph stages: pipeline runs; conversion: `ConversionOutput`).
  Descriptor operations receive the resolved request principal and preserve each stage's own principal-scoping contract (conversion: owner-scoped like its JSON API; graph stages: none exists).
- **Bulk-action semantics**: skip-and-report ineligible rows, uniformly — both existing baseline specs already require the mixed-eligibility summary, so the monitor adopts it as the single contract.
- **Status display**: stage-native statuses are what operators see and what action eligibility keys on (`FAILED_RETRYABLE` vs `FAILED_PERM` is operator-meaningful); the generic lifecycle is the rollup vocabulary for the dashboard and the cross-stage filter.
- **Conversion helper lift**: `_apply_job_retry`/`_apply_job_cancel` move from `aizk.conversion.api.routes.jobs` to a conversion domain module; the JSON API and the console both call the lifted helpers.
- **Dashboard**: per-stage status counts from each descriptor's count query over its work-unit table — project-owned projections, no orchestrator internals; the failed category distinguishes awaiting-retry from permanent, so repairability is visible at a glance.
- **Boundary validation**: unknown stage/unit → not-found, undeclared action → rejected, empty selection → no-op, bulk selection bounded to the JSON API's existing cap so write transactions stay short.
- **Conversion app root**: with `/ui/jobs` gone, the conversion app root redirect needs a new target (its own `/docs`) — decided in `design.md`.

## Schema Impact

- `conversion-api-openapi`: the `/ui/jobs*` HTML paths (currently included in the schema) are **removed**.
  No JSON route is added, changed, or removed.
- The console app (graph operator app) is not schema-tracked; no new tracking is added by this change.
