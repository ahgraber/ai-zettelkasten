# Design: operator-console

## Context

Two FastAPI apps exist today.
The conversion service (`aizk.conversion.api.main`) owns the heavy submission runtime and serves one HTML jobs page beside its JSON API.
The graph operator app (`aizk.graph.api.main`) is light — no docling/torch, no S3 submission runtime — already imports the shared `ConversionConfig`/`AuthSettings`, sits behind the same trusted-host perimeter, reads the shared database, and serves two stages' HTML UIs (contextualization, extraction) built by mirroring each other.

The stage runtimes are already unified underneath: all three stages run on the shared pipeline runner, and all three record lifecycle events in the shared `pipeline_events` table, queryable per-unit by `(stage, work_unit_ref)`.
The remaining divergence is read-side: conversion persists its own status enum (`ConversionJobStatus`, with `UPLOAD_PENDING` and a split failed pair), while graph stages persist the generic `WorkUnitStatus`.

Constraints from the project architecture rules:

- Operator surfaces query project-owned projections; the console is a read model plus thin command dispatch — it never claims, leases, schedules, or executes.
- The single-writer SQLite discipline (short `BEGIN IMMEDIATE` transactions) is preserved; the console's only writes are the existing retry/cancel transitions, which the graph app already performs today.
- No frontend framework change: server-rendered Jinja + HTMX stays (Datastar/SSE evaluation is a separate ADR, per the proposal's out-of-scope list).

In-flight work absorbed by this change: shared shell partials (`_nav.html`, `_styles_base.html`, `_styles_jobs.html`) and navigation tests already exist uncommitted on this branch.

## Decisions

### Decision: EvolveGraphAppIntoConsole

**Chosen:** the existing graph operator app becomes the console — it gains the conversion stage's monitoring UI and is retitled; the conversion service drops its `ui` router and templates and keeps JSON only.

**Rationale:** the graph app is already the de-facto console: light dependencies, shared config/auth/DB, two of three stages hosted.
Evolving it pays no new app-scaffolding or deployment cost, and the conversion service sheds UI concerns that don't belong beside its submission runtime.

**Package home:** the generic, cross-stage console code (descriptor contract and registry, task-monitor and dashboard routes, generic templates) lives in a top-level `aizk.console` package rather than under `aizk.graph.api`, because it monitors conversion as well as the graph stages — it is a presentation layer *above* both domains, not a graph-owned surface.
The graph app's `create_app()` mounts the `aizk.console` router; this is code organization only, not a new app, process, deployment, or config wiring (the rejected "new dedicated app" alternative).
The graph-specific explorer stays in `aizk.graph.api` (it browses graph chunk/contextualization artifacts) and is mounted into the console shell.
The app entrypoint (`aizk.graph.api.main`) and its CLI/proctitle are unchanged; relocating or renaming the app module, and lifting the shared auth/config kernel out of `aizk.conversion`, are deferred to a separate change.

**Alternatives considered:**

- New dedicated `aizk.console` app: re-pays scaffolding, deployment, and config wiring the graph app already has; adds a third process for no capability gain.
- Keep per-service UIs behind a reverse proxy: preserves the duplication and the two-owners problem; the cross-stage monitor would still need one app importing another stage's domain code — the same coupling as consolidation, plus a proxy.

### Decision: SinglePrincipalOperatorPosture

**Chosen:** the console is a deployment-global operator surface under the single-principal `trust_network` model, and it preserves each stage's own principal-scoping contract rather than inventing one.
Descriptor list/count/detail/action operations receive the resolved request `Principal`; the conversion descriptor applies the same `owner_id == principal.subject` filter its JSON API contract applies (a no-op today, since `trust_network` is single-principal by design) and inherits the API's cross-owner posture (not-found for detail, per-unit `job_not_found` in bulk).
Graph stages define no principal-scoping (their work-units carry no owner), so the console adds none.

**Rationale:** every implemented auth mode resolves exactly one principal per deployment, so no cross-owner boundary exists to violate — but the conversion API's contract _shape_ is owner-scoped, and the console should not silently diverge from it.
Plumbing the principal is one parameter on operations whose routes already resolve it for the perimeter requirement.
If a multi-principal auth mode (`token`, `proxy_headers`, `oidc`) is ever implemented, console access policy (operator/administrator role) is part of that auth-model change — the baseline conversion-api spec already assigns such widening to a future delta on the `Principal` type and resolver.

**Alternatives considered:**

- Owner-scope the whole console: incoherent — graph work-units have no `owner_id`; ownership lives in provenance.
- Omit the principal from descriptor operations: observationally identical today, but silently diverges from the conversion API's owner-scoped list/count contract and forecloses the parity tests.

### Decision: StageDescriptorRegistry

**Chosen:** a `StageDescriptor` dataclass registered per stage, from which the console derives everything stage-specific.
Fields:

- `key`, `label` — stage identity in URLs, navigation, and the dashboard.
- `list_units(principal, ...)` / `count_by_status(principal)` — the stage's list query (filter/search/pagination inputs) and dashboard count query over its own work-unit table, scoped per SinglePrincipalOperatorPosture.
- `columns_template` — the per-stage columns partial the generic monitor template includes.
- `native_statuses` + `rollup` — the display vocabulary and the native→`WorkUnitStatus` mapping (see StatusAdapterReadSide).
- Search over stage-declared identifiers (e.g. conversion's KaraKeep id and job title) is owned by `list_units`, which the design already charters with the stage's "filter/search/pagination inputs" — so it is folded into that query closure rather than carried as a separate `searchable_identifiers` field the routes would re-implement.
- `actions` — the stage's **declared** actions only: an apply callable dispatching to the stage's existing domain helper; the monitor offers exactly the declared set.
  Per-action eligibility is encoded in that apply callable (it raises `ValueError` in the unit's current status, exactly as each stage's JSON API already does) rather than a separate predicate field, so the mixed-eligibility skip-and-report needs no second source of truth.
  Graph stages declare Retry and Cancel; conversion additionally declares Delete, preserving the destructive action its retired HTML UI offered (see ConversionHelperLift).
- `detail(principal, ...)` — the **optional** drill-down detail composer: the shared event trail renders for every stage; the runs/artifact section only when declared (graph stages: pipeline runs; conversion: `ConversionOutput`).

One generic set of console routes (`/ui/tasks`, `/ui/tasks/{stage}/{id}`, actions endpoint) and one monitor template render any registered descriptor; the registry is a module-level ordered mapping.

**Rationale:** the three existing jobs pages are structural clones differing exactly along these fields — the descriptor is the measured divergence, nothing more.
A registered test-double stage makes the `cheap-stage-onboarding` contract directly testable.

**Alternatives considered:**

- ABC/subclass hierarchy per stage: more ceremony for the same seam; a data-shaped descriptor keeps the console's routes the single owner of control flow.
- Keep per-stage routes and only share templates: leaves the ~200-line route duplication the proposal measures, and the registry contract ("operable without console modification") would be false.

### Decision: StatusAdapterReadSide

**Chosen:** stage-native statuses are what the monitor displays and what action eligibility keys on; the generic lifecycle is a read-side rollup used by the dashboard.
Conversion's mapping: `NEW`→queued, `QUEUED`→queued, `RUNNING`→running, `UPLOAD_PENDING`→running, `SUCCEEDED`→succeeded, `FAILED_RETRYABLE`→failed, `FAILED_PERM`→failed, `CANCELLED`→cancelled.
Graph stages map by identity.
The dashboard's failed category subdivides by repairability: conversion via the `FAILED_RETRYABLE`/`FAILED_PERM` status pair; graph stages via `FAILED` with a present vs `NULL` `earliest_next_attempt_at` (the runtime's retry-class projection).
An exhaustiveness test asserts every `ConversionJobStatus` member has exactly one rollup target, so a future native status cannot silently vanish from the dashboard.

**Rationale:** `FAILED_RETRYABLE` vs `FAILED_PERM` and `UPLOAD_PENDING` are operator-meaningful — collapsing them for display would make the unified page worse than the pages it replaces.
Read-side mapping delivers every dashboard/rollup benefit with zero storage migration.

**Alternatives considered:**

- Migrate conversion storage to the generic enum: production data migration, claim-query and JSON-API contract churn, and the loss (or column exile) of `UPLOAD_PENDING` — all for a benefit the read-side map already provides.
- Derive status from `pipeline_events`: the status projections exist and are indexed; re-deriving them per request adds cost and a second source of truth.

### Decision: ConversionHelperLift

**Chosen:** `_apply_job_retry` / `_apply_job_cancel` / `_apply_job_delete` (and their eligibility sets) move from `aizk.conversion.api.routes.jobs` into a new domain module `aizk.conversion.job_actions` as public `apply_job_retry` / `apply_job_cancel` / `apply_job_delete`; the conversion JSON API and the console's conversion descriptor both call the lifted helpers.
Behavior is unchanged; the console's bulk actions run them inside the same short `BEGIN IMMEDIATE` transaction shape the graph UI actions use today.

Delete is lifted alongside retry/cancel because it is a genuine operator job-action, not route glue: it was reachable only from conversion's HTML UI and, when that UI is removed, would otherwise be dropped silently.
Delete differs from retry/cancel in kind — it is a hard removal of the job row and its `ConversionOutput`, records no lifecycle event, and has no JSON-API pathway (the bulk endpoint is retry/cancel only).
So the action-equivalence requirement below is scoped to retry/cancel, which have API peers; delete is a console-only capability whose spec cover asserts its removal effect and its terminal-status eligibility, not API parity.

**Rationale:** the console must import domain code, never another app's route module.
A dedicated module keeps cohesion clean: `aizk.conversion.queries` charters the runner-facing claim/stale-recovery helpers, and operator commands are a different consumer with a different caller set.
Lifting all three job-actions together keeps the mental map single: every operator command over a conversion job lives in `job_actions`, none stranded in a route module.
The action-equivalence spec requirement pins retry/cancel: console pathway and API pathway must produce identical transitions and events.

**Alternatives considered:**

- Lift into `aizk.conversion.queries`: overloads a module whose charter is the engine-facing claim/recovery queries; operator commands would blur that boundary.
- Console calls the conversion JSON API over HTTP: adds a hop, an auth story, and a failure mode between two processes reading the same database — for writes the console can make directly under the existing writer discipline.
- Import the route helpers as-is: couples the console to FastAPI route-module internals and blocks the conversion app from ever dropping those modules.

### Decision: ConsoleBoundaryBounds

**Chosen:** boundary validation happens before any stage data is touched: unknown stage key or unit id → 404; unrecognized or undeclared action → 400; empty selection → no mutation, informative flash; bulk selection capped at 100 ids (matching `BulkJobActionRequest`'s `1–100` bound), rejected whole above the cap.

**Rationale:** the cap keeps `BEGIN IMMEDIATE` write transactions bounded (the same reason the JSON API bounds its bulk endpoint), and per-boundary validation is the project's external-boundary rule applied to the console's one mutating surface.

**Alternatives considered:**

- Unbounded HTML selections (status quo of the current UIs): a full-table select-all would hold the write lock for an unbounded batch.
- Truncate oversized selections silently: violates the no-silent-caps observability posture; reject-whole is unambiguous.

### Decision: UrlStructureAndRoots

**Chosen:** console URL space: `/ui` (dashboard), `/ui/tasks?stage={key}` (monitor), `/ui/tasks/{stage}/{id}` (drill-down), `/ui/explore/chunks` (explorer, moved from `/ui/graph/jobs`-era paths).
Console root `/` redirects to `/ui`.
Old per-stage paths (`/ui/graph/jobs`, `/ui/graph/extraction-jobs`, `/ui/graph/explorer`, conversion's `/ui/jobs`) are retired without redirects.
The conversion app's root redirect retargets to its own `/docs`.

**Rationale:** the unified paths make the registry visible in the URL space and are what the descriptor contract generates; redirects for an internal single-operator tool are speculative scope.
`/docs` is the conversion app's only remaining human-facing surface, so its root points there.

**Alternatives considered:**

- Keep existing paths per stage: encodes the pre-consolidation shape permanently and forces per-stage route registration, contradicting the registry contract.
- Legacy redirects: no external consumers exist; YAGNI.

### Decision: TemplateComposition

**Chosen:** one generic monitor template (list + filters + bulk-action form + pagination) that includes the descriptor's `columns_template`; one generic drill-down template that renders the event trail and includes the descriptor's detail section; the in-flight shell partials (`_nav.html`, `_styles_base.html`, `_styles_jobs.html`) become the console shell.
The three existing jobs templates (`jobs.html`, `extraction_jobs.html`, conversion's) collapse into the generic pair; HTMX fragment endpoints keep serving panel partials without the nav shell.

**Rationale:** the templates are where the copying is worst (174 duplicated lines already removed by the in-flight styles extraction); the columns partial is the only honest per-stage residue.

**Alternatives considered:**

- Shared partials only, per-stage full templates kept: the next stage still copies a full template; the "no console template modification" contract would fail.

## Architecture

```text
aizk operator console (the retitled graph operator app, one origin)
│
├── JSON routers: graph + extraction operator APIs        (unchanged)
│
├── /ui console routes (generic, descriptor-driven)
│     /ui                      dashboard ── for each descriptor: count_by_status() → rollup
│     /ui/tasks?stage=k        monitor   ── registry[k].list_units() + columns_template
│     /ui/tasks/{k}/{id}       drill-down── pipeline_events(stage=k, work_unit_ref=id)
│     │                                     + registry[k].detail()
│     /ui/tasks/{k}/actions    bulk retry/cancel ── registry[k].actions (skip-and-report)
│     /ui/explore/chunks       explorer (existing routes, moved path, same contracts)
│
└── StageDescriptor registry
      conversion:        ConversionJob · native enum + rollup map · lifted
                         apply_job_retry/apply_job_cancel · ConversionOutput detail
      contextualization: ContextualizationJob · identity rollup · _apply_retry/_apply_cancel
                         · chunking/summary/contextualization runs detail
      extraction:        ExtractionJob · identity rollup · extraction-run detail

conversion service app: JSON API only (ui router + templates removed; / → /docs)

shared SQLite (short BEGIN IMMEDIATE writes; both apps already write today — unchanged)
```

## Risks

- **Regression while collapsing three pages into one monitor**: the existing UI integration tests are ported to the unified paths (not rewritten), and the action-equivalence tests pin behavior against each stage's own pathway.
- **Rollup map drift when a native status is added later**: the exhaustiveness test over `ConversionJobStatus` members fails on any unmapped status.
- **Import creep from console into conversion internals**: the conversion descriptor imports only `aizk.conversion.datamodel` and `aizk.conversion.queries`; route, wiring, and processing modules stay off-limits (review guard, asserted by an import-boundary test).
- **Stale references to retired paths**: a whole-tree sweep (`rg` across src, tests, templates, docs, notebooks) for the old `/ui/graph/*` and conversion `/ui/jobs` paths before completion.
- **Monitor/dashboard latency on large tables**: list and count queries group over the existing indexed status columns; the 1000-unit load scenario is exercised by a seeded timing test.
