# Tasks: Pipeline Work Admission

Groups are ordered by build dependency.
Groups 4–6 are mutually independent once groups 1–3 exist.

## 1. Schema baseline

- [x] Add a `graph-api-openapi` generator entry to `.specs/.sdd/schema-config.yaml` alongside the conversion entry.
- [x] Capture `schemas/before/` snapshots for both APIs and write `schemas/expected.md` (graph gains the two intake operations; conversion unchanged).
  Requires network for `uv run`; run outside the sandbox if it refuses.

## 2. Capacity at the enqueue seam

- [x] Add capacity config fields to the graph config (`contextualization_queue_max_depth`, `extraction_queue_max_depth`, `queue_retry_after_seconds`; unset/0 = no limit), with a hermetic `_env_file=None` settings test.
- [x] Define the capacity-refusal exception type in `aizk.graph`.
- [x] Enforce capacity in `enqueue_document` after the dedupe branch.
  Tests in `tests/graph/test_enqueue.py`: new unit refused at capacity; duplicate at capacity returns the existing unit; no configured limit accepts.
- [x] Enforce capacity in `enqueue_extraction` after the dedupe branch.
  Tests in `tests/graph/test_extraction_workunit.py`: the same three cases (second construction site needs its own evidence).
- [x] Compute per-batch headroom in `enqueue_backfill_outputs`, `enqueue_backfill`, and `enqueue_extraction_backfill` instead of per-row checks.
  Tests: a bulk enqueue over more work than headroom admits only the headroom and leaves the remainder unenqueued (one test per bulk write-site).
- [x] Pass each stage's configured limit through the backfill runs to the bulk command surface, and map the refusal to a non-zero exit with a message.
  Tests: each command forwards its own stage's limit; a refusal exits non-zero without a traceback.

## 3. Pending-work derivations

- [x] Contextualization pending query, generalizing `latest_output_ids_per_source` with an anti-join on work-units.
  Partition tests: never-contextualized pending; unit-for-newest-output not pending; re-converted pending again; no-conversion-output not pending.
- [x] Extraction pending query, generalizing `_sources_with_active_chunking_run` with an anti-join on work-units.
  Partition tests: chunked-never-extracted pending; unit-in-any-status (including terminal) not pending; re-chunked-with-succeeded-unit not pending; no-chunking-run not pending.
- [x] State-only evidence for both queries: evaluating twice against identical state yields the identical set.

## 4. Staleness derivation (extraction)

- [x] Staleness query: active mention-extraction run's recorded `upstream_derivation_key` vs. the key `_resolve_upstream_derivation_key` yields for current state.
  Tests: re-chunked source stale; raw-extracted source stale after an active contextualization run appears; current source not stale.
- [x] Resolver-conformance test pinning the staleness query and the execute-time resolver to the same verdict on the same state.
- [x] Explicit stale-is-not-pending test: a stale source does not appear in the extraction pending set.

## 5. Admission loop in the workers

- [x] Per-stage admission adapter (pending query + enqueue + capacity + enable flag) for both graph stages, with a queryable-declaration test (declaring stage reports it; absent adapter reports none).
- [x] Admission enable/interval config fields (`admission_contextualization_enabled`, `admission_extraction_enabled`, `admission_interval_seconds`; default off), with a hermetic settings test.
- [x] Admission loop in the contextualization worker; wiring covered in `tests/graph/test_worker_build.py`.
- [x] Admission loop in the extraction worker; wiring covered in `tests/graph/test_extraction_worker_build.py`.
- [x] Behavior tests (new `tests/graph/test_admission.py`):
  - disabled stage with pending work: nothing created
  - enabled stage: pending work admitted with no operator action
  - per-stage isolation: enabling one stage admits nothing for the other
  - a pass admits exactly the pending set; non-pending sources untouched
  - re-running a pass without state change creates nothing
  - an admitted unit is identical to a manually enqueued unit for the same work
  - a pass stops at capacity; after the backlog drains, a later pass admits the remainder
  - work left unadmitted is still pending on the next evaluation
- [x] Loop lifecycle test: worker shutdown drains the admission loop cleanly, wrapped in the appropriate pyleak guard (`no_thread_leaks` / `no_task_leaks`).

## 6. Intake routes on the graph service

- [x] Request/response schemas for intake, mirroring the conversion API's queue-full response shape.
- [x] `POST /v1/contextualizations` resolving a conversion-output reference to the domain enqueue.
  Tests in `tests/graph/test_operator_api.py`: 201 on create; 200 with the existing unit on resubmission; 404 on unknown output with no state change; 503 + `Retry-After` at capacity; 200 for a duplicate at capacity.
- [x] `POST /v1/extractions` resolving a source identity to the domain enqueue.
  Tests in `tests/graph/test_extraction_operator_api.py`: the same matrix with 404 on unknown source.
- [x] Equivalence tests, one per stage: an intake-created unit is identical to a domain-enqueued unit for the same work.

## 7. Extraction re-admission

- [x] Requeue action in `job_actions.py`: eligible when terminal and source stale; transitions to `QUEUED` with attempts cleared and a requeue event co-committed.
  Tests: transition and event recorded; non-stale terminal unit skipped unaltered; non-terminal unit skipped unaltered.
- [x] Declare the `Re-extract` action on the extraction stage's console descriptor.
  Console test: bulk application over a mixed selection applies to eligible units and reports the rest skipped.
- [x] End-to-end test with the stub extractor: a re-admitted stale source re-executes reading current active inputs and the new run supersedes the prior one.
- [x] Update the `extraction_workunit` module docstring: the re-trigger deferral is decided — describe the requeue mechanism that now exists.

## 8. Console visibility

- [x] Optional `pending_count` / `pending_list` / `stale_count` callables on `StageDescriptor`, feature-detected like `failed_split`; descriptor tests in `tests/console/test_descriptors.py`.
- [x] Dashboard pending column.
  Tests: declaring stage shows its pending count; per-status counts and unit totals are unchanged by pending sources; conversion (non-declaring) shows no pending figure.
- [x] Pending-source listing on the stage's monitor page, using the monitor's title contract.
  Tests: listed set matches the derivation; listed count equals the dashboard count; non-declaring stage offers no listing.
- [x] Dashboard stale count and monitor stale marking for extraction.
  Tests: stale count shown; stale units identifiable and selectable together for a declared action; non-declaring stage shows no stale figure or marking.

## 9. Docs and closure

- [x] Update `src/aizk/graph/README.md` (admission, intake, capacity, re-admission) and the conversion README's downstream note if its hand-off description is now stale.
  The conversion README describes only the identities it mints, not a hand-off mechanism, so it needed no change.
- [x] Capture `schemas/after/` snapshots and check the diff against `schemas/expected.md`.
  Conversion unchanged; graph gained the two `post` operations and the three request/refusal schemas, as expected.
- [x] Full validation: `uv run pytest -n auto -m "not integration_lifecycle" tests/`, then `uv run pytest -m integration_lifecycle tests/`, then the pre-commit hooks over everything changed since `HEAD`.
