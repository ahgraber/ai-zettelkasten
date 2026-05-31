# Tasks: Pipeline-Stage Runtime

> Build order: primitives (run + state machine + transition helper + handler protocol) → runner over the protocol → port conversion onto the primitives (behavior-preserving) → final spec reconcile.
> Conversion's existing test suite is the regression net for the port. Runner tests run against a stub repository and wrap the act phase with `pyleak` (`no_task_leaks` / `no_thread_leaks`) per the project testing rule. Run via `uv run pytest tests/pipeline/`; conversion lifecycle tests via `uv run pytest -m integration_lifecycle`.

## Primitives foundation (`aizk.pipeline`)

- [x] Create the `aizk.pipeline` package and define the generic lifecycle types: statuses `queued | running | succeeded | failed | cancelled | timed_out` and a `retryable | permanent` classification for failures.
- [x] Define the `StageHandler` protocol (discover/claim eligible work-units, transition status, map result→terminal outcome + retry class, cancellation hook, cleanup, startup dependency validation, timeout config, run `scope_key`) — the seam from `design.md` § StageHandlerAndAdapterResponsibilities.
- [x] Implement the run primitive: a `run` record keyed by `(stage, scope_key)` with version stamps, `input_fingerprint`, `supersedes_run_id`, `status`; enforce "≤1 active per `(stage, scope_key)`" with a partial unique constraint; implement atomic activate-new + supersede-prior in one transaction.
- [x] Implement the `record_transition` helper and the shared append-only `pipeline_events` table (stage, work-unit ref, run ref, `aizk_uuid` source identity, typed per-kind payload); the helper co-commits the event with the status change.
- [x] Add an Alembic migration for the `run` and `pipeline_events` tables.
- [x] Test `tests/pipeline/test_run_primitive.py::test_atomic_supersede`, `::test_concurrent_runs_one_active`, `::test_failed_supersession_changes_nothing`. **[R: runs invalidated atomically per (stage, scope_key)]**
- [x] Test `tests/pipeline/test_transitions.py::test_status_and_event_co_committed` and `::test_failed_transaction_leaves_neither`. **[R: atomic durable transitions]**
- [x] Test `tests/pipeline/test_transitions.py::test_events_resolvable_by_source_across_stages`. **[R: cross-stage source identity]**
- [x] Test `tests/pipeline/test_lifecycle.py::test_single_terminal_outcome_classified` and `::test_only_retryable_eligible`. **[R: generic lifecycle + retry classification]**
- [x] Test `tests/pipeline/test_migrations.py`: migrated schema structurally equivalent to the ORM baseline. **[schema fidelity]**
- [x] **(portability correction — W1)** The active-run partial unique index is declared with `sqlite_where` only ([run.py:62](../../../src/aizk/pipeline/run.py#L62) and `migrations/versions/d0e1f2a3b4c5_*.py`), so on any non-SQLite dialect SQLAlchemy silently drops the `status='active'` predicate and the index degrades to a **full** unique on `(stage, scope_key)` — which forbids a superseded row coexisting with a new active one, breaking supersession on the Postgres engines ADR-009 names as cheapest next steps (procrastinate/absurd).
  Add `postgresql_where=text("status = 'active'")` alongside `sqlite_where` in both `run.py` and the Alembic migration so the "≤1 active per `(stage, scope_key)`" invariant is dialect-agnostic (defensive zero-risk fix; full Postgres test deferred under ADR-003's SQLite-only target).
  Re-run `tests/pipeline/test_run_primitive.py` + `test_migrations.py` to confirm SQLite behavior is unchanged.
  See [orchestration-flex-exploration.md](orchestration-flex-exploration.md) (W1).

## Runner over the handler protocol

- [x] Implement the runner loop driving a `StageHandler`: claim eligible work-units, bounded concurrency, eligible-in-submission-order, retry-wait gating.
- [x] Implement graceful drain on termination signal (stop claiming, bounded drain timeout, none left running), cancellation (running ≤ bounded interval; queued-cancelled skipped), wall-clock timeout (→ `timed_out`, graceful-before-forceful, no orphan descendants), transient-resource cleanup on every outcome, and stale-unit recovery recording its cause.
- [x] Implement startup dependency validation gating work acceptance, structured logs with trace context, operational metrics, and `setproctitle` stage-role identification.
- [x] Build a stub `StageHandler` (in-memory) for runner tests.
- [x] Test `tests/pipeline/test_runner_adapter.py::test_new_stage_runs_via_adapter` and `::test_two_stores_share_runner`. **[R: runner over handler protocol]**
- [x] Test `tests/pipeline/test_runner_scheduling.py::test_concurrency_within_limit_in_order` and `::test_retry_wait_gates_eligibility` (wrap act with `no_task_leaks`). **[R: eligible-in-submission-order + bounded concurrency]**
- [x] Test `tests/pipeline/test_runner_drain.py::test_inflight_finishes_during_drain` and `::test_drain_timeout_enforced` (wrap with `no_task_leaks`/`no_thread_leaks`). **[R: graceful drain]**
- [x] Test `tests/pipeline/test_runner_cancel.py::test_running_cancelled_promptly` and `::test_queued_cancel_skipped`. **[R: cancellation]**
- [x] Test `tests/pipeline/test_runner_exec.py::test_timeout_recorded_no_orphans` and `::test_cleanup_on_every_outcome` (wrap with `no_thread_leaks`). **[R: bounded execution + cleanup]**
- [x] Test `tests/pipeline/test_runner_recovery.py::test_stale_unit_recovered_with_cause`. **[R: stale-unit recovery]**
- [x] Test `tests/pipeline/test_runner_startup.py::test_missing_dependency_blocks_acceptance`. **[R: startup validation gates acceptance]**
- [x] Test `tests/pipeline/test_runner_observability.py::test_lifecycle_logs_metrics_and_role`. **[R: lifecycle observability + stage-role]**

## Runner hardening (review round 2)

> Findings from a runtime-boundary review: process-global side effects, concurrency-guarantee wording, and partial-failure handling. Additive to the runner group; no `aizk.conversion` changes.

- [x] **(F1, blocking)** Signals reach only one runner: `install_signal_handlers` binds a process-wide handler to a single `ShutdownController` (and no-ops off the main thread), so two runners sharing a process cannot both observe SIGTERM/SIGINT — contradicting "two stages share a process."
  Add a process-level signal dispatcher that broadcasts shutdown to all registered controllers; test two controllers both observe one signal.
- [x] **(F1, doc)** Name the multi-runner semantics of the other process-global effects: `force_exit`/`os._exit` terminates the whole process (all runners); `setproctitle` is last-write in a shared process.
- [x] **(F2)** `test_concurrency_within_limit_in_order` asserts scheduler-dependent worker start order; assert the durable guarantee instead — claim/selection order (the first `limit` claimed are the first `limit` submitted) — and clarify in the runner docstring that "begin in submission order" means claim/dispatch order, not serialized thread starts (no start handshake).
- [x] **(F3, blocking)** Finalize DB failure strands completed work: on `OperationalError`/`DBAPIError` in `finalize` the runner pops the slot + cleans up while the unit stays `running` in the DB (delayed recovery + duplicate execution).
  Keep the slot (do not pop/cleanup) until the durable terminal transition lands so the reap loop retries it; regression test — finalize fails once then succeeds → the unit reaches its terminal status, not stranded.
- [x] **(F4)** Stub repo re-claims permanent failures: `finalize` clears `earliest_next_attempt_at` for permanent failures and `_ELIGIBLE_STATUSES` treats any FAILED as eligible.
  Persist retry class (or a distinct permanent-failed status) so only retryable-failed is eligible; test that a permanent failure is not re-claimed.

## Pre-port runtime invariant gates

> Gate the conversion port on proving the runtime-boundary failure class, not only the concrete review findings. Required before wiring conversion to the shared runner or marking the port green.

- [x] **(G1: process-global ownership)** Document and test ownership/broadcast semantics for process-global effects touched by the runner: SIGTERM/SIGINT, `os._exit`, `setproctitle`, process logging setup, thread-pool lifecycle, and `atexit` behavior.
  Include a two-runner-in-one-process shutdown test.
- [x] **(G2: concurrency contract precision)** Rewrite any "submission order" wording/tests into the exact durable guarantee being provided (claim order, submit order, worker-start order, or completion order), and enforce/test a start-order handshake only if worker-start order is required.
- [x] **(G3: durable scheduling state)** Ensure every scheduling decision uses durable, non-lossy state: retryability, retry-wait, stale-recovery cause, terminal outcome, and generation/run identity where applicable.
  Add regression coverage proving permanent failures are not retried and retryable failures are gated by retry wait after process restart/reconstruction.
- [x] **(G4: partial-failure finalization)** Fault-inject finalize/transition DB lock and error paths; prove completed work is not forgotten until the durable terminal transition lands or an explicit recoverable state is persisted.
  Include cleanup behavior in the assertion.
- [x] **(G5: lifecycle fault-injection matrix)** Add or identify tests for signal during drain/finalize, timeout during subprocess cleanup, cancellation during DB contention, stale recovery after interrupted execution, and two runners sharing one process.

## Real-world E2E notebook gate

> Manual confidence gate for real external-service execution. Keep it out of standard pytest.

- [x] Add a `# %%`-delimited real-world conversion smoke script under `notebooks/` (not `tests/`) so standard `uv run pytest tests/` never collects it.
  The script must create a temp SQLite file, set `AIZK_DATABASE_URL=sqlite:///<tmpfile>` before constructing conversion config/app/worker objects, run migrations against that temp DB, and leave the normal `data/conversion_service.db` untouched.
  Its module docstring must concisely state required environment variables (`AIZK_FETCHER__KARAKEEP__API_KEY`, `AIZK_FETCHER__KARAKEEP__BASE_URL`, and any optional Docling/VLM vars), how to verify them, when to run the notebook, and that it performs real network/provider work.
- [x] Add notebook cells that submit a small bounded KaraKeep sample, run the shared-runner worker against the temp DB, and print/validate observable outcomes: job status counts, terminal outcomes, output rows/artifacts, and pipeline events for at least one source.

## Migration-snapshot test gates

> Automated migration confidence gates under `tests/`. Keep checked-in DB fixtures small, generated, and free of real user data or secrets.

- [x] Add a migration-snapshot strategy rather than one fixture before every revision: keep targeted per-migration synthetic tests for data-transform edge cases, and add at most one generated/sanitized legacy SQLite snapshot under `tests/conversion/fixtures/` that upgrades from a representative pre-head state to head.
- [x] Define the checked-in legacy snapshot shape: small (less than 5 MB) and data-diverse rather than large; include roughly 20-50 sources/jobs/outputs/events covering all conversion statuses, retry-wait states, attempts, owner/idempotency variations, deleted-job/orphan event rows, nullable-to-non-null backfill cases, and representative payload JSON.
- [x] Add a pytest migration test under `tests/conversion/integration/` that copies the generated legacy snapshot to `tmp_path`, upgrades the copy to head, verifies schema version/head, row counts/content invariants, idempotency uniqueness, event relocation, source audit lookup by `aizk_uuid`, and reruns safely on an already-upgraded copy.

## Port conversion onto the primitives (behavior-preserving)

> This group is surgery, not file moves. The runner logic in `workers/` is interleaved with the conversion unit-of-work; carve the unit-of-work into the adapter **before** moving the loop. Sub-steps below capture the known hazards from the implementability review.

- [x] **(carve-out, do first)** Extract the conversion unit-of-work from `orchestrator.py` (`process_job_supervised`, the upload phases / `UPLOAD_PENDING` transition, source-enrichment, `SubprocessMetadata` parsing) into `ConversionStageHandler.execute`/`finalize`, so the generic loop has a clean adapter to call.
  The orchestrator today is shell + unit-of-work fused; this is the substantial step.
- [x] Implement `ConversionStageHandler` over `ConversionJob`, mapping conversion statuses onto the generic lifecycle: `UPLOAD_PENDING` stays a conversion-private status mapped to generic `running` (the runner never sees a "running sub-state"); conversion's timeout maps to generic `failed`/retryable (preserving today's `FAILED_RETRYABLE`-on-timeout behavior — conversion exercises only a subset of the generic terminal set); `NEW` remains never-committed.
  Keep owner-scoped idempotency, source-ref, upload phases, and output links in `aizk.conversion`.
- [x] **(hazard F5)** Preserve the per-site `attempt` semantics and the `(aizk_uuid, attempt)` snapshot taken at supervision entry (API submit `attempt=0` with `from_status=None`; claim = post-increment; cancel = no-increment) when routing transitions through the generic helper, so retry-history scenarios stay green.
- [x] Route conversion status transitions through the shared `record_transition` helper and relocate `conversion_job_events` into `pipeline_events` (Alembic migration), genericizing the typed columns per `design.md` § PipelineEventsGenericSchema.
- [x] **(hazard F3)** Convert the module-global shutdown state (`workers/shutdown.py` `_shutdown_event`/`_signal_count`) into per-runner-instance state, preserving the `os._exit` force-exit-bypasses-pool-join behavior; signals stay process-wide but drain bookkeeping is per-instance (so two stages can share a process).
- [x] Move the (now generic) runner loop out of `aizk.conversion.workers.{loop,orchestrator,shutdown,supervision}` into `aizk.pipeline`, delete the conversion-local duplicates, and wire the conversion worker entrypoint to run the shared runner with `ConversionStageHandler`.
- [x] **(hazard F2)** Update `tests/.../test_no_direct_status_writes.py`: it asserts the only `.status = ConversionJobStatus.` write lives in `conversion/datamodel/events.py`; after the port, status writes flow through the generic helper and the `ConversionStageHandler`, so relocate/relax the guard to track the new sanctioned write path rather than failing the regression net.
- [x] Verify the conversion suite is green unchanged (regression net): `uv run pytest -n auto -m "not integration_lifecycle" tests/conversion` and `uv run pytest -m integration_lifecycle tests/conversion`.
- [x] Test `tests/conversion/.../test_event_relocation_equivalence.py`: after relocation, the events recorded for a conversion job match the pre-relocation event set (values), and the audit-survives-job-deletion behavior is preserved via `aizk_uuid`. **[behavior preservation]**

## Final spec reconcile (after the port is green)

- [x] Relocate the now-duplicated generic contracts out of `worker-process-management` and `conversion-worker` into `pipeline-stage-runtime`: add MODIFIED/REMOVED deltas for the relocated requirements with `> Previously:` provenance, leaving the conversion specs holding only conversion-specific behavior.
  Update the change's delta specs accordingly.
- [ ] **(do BEFORE sync)** Align `conversion-worker` spec prose with the ADR-009 naming convention (orchestration = engine layer only).
  Replace the ~12 lowercase "orchestrator" actor-labels in `.specs/specs/conversion-worker/spec.md` per their sense: the fetch/convert _coordination_ usages (input inspection, converter resolution, conversion invocation) → "the conversion coordinator"; the _transition_ usages ("the orchestrator transitions the job to FAILED_PERM/UPLOAD_PENDING/...") → "the stage handler" (those moved to `ConversionStageHandler` in this change).
  The implementation rename already landed (`Orchestrator`→`ConversionCoordinator`, `workers/`→`processing/`); this closes the spec-vs-code drift so the baseline is congruent at sync time.
  See `docs/decision-record/009-orchestration.md` § Naming convention.

## Documentation / decision record

- [x] Add `src/aizk/pipeline/README.md` documenting the primitives (runner, handler protocol, run primitive, transition helper, lifecycle/state machine) and how a stage consumes them.
- [x] Record the primitives decision as an addendum to `docs/decision-record/009-orchestration.md` (brainstorming/design pass; may lag the specs).
