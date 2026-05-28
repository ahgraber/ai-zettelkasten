# Tasks: Pipeline-Stage Runtime

> Build order: primitives (run + state machine + transition helper + repository protocol) → harness over the protocol → port conversion onto the primitives (behavior-preserving) → final spec reconcile.
> Conversion's existing test suite is the regression net for the port. Harness tests run against a stub repository and wrap the act phase with `pyleak` (`no_task_leaks` / `no_thread_leaks`) per the project testing rule. Run via `uv run pytest tests/pipeline/`; conversion lifecycle tests via `uv run pytest -m integration_lifecycle`.

## Primitives foundation (`aizk.pipeline`)

- [x] Create the `aizk.pipeline` package and define the generic lifecycle types: statuses `queued | running | succeeded | failed | cancelled | timed_out` and a `retryable | permanent` classification for failures.
- [x] Define the `StageRepository` protocol (discover/claim eligible work-units, transition status, map result→terminal outcome + retry class, cancellation hook, cleanup, startup dependency validation, timeout config, run `scope_key`) — the seam from `design.md` § StageRepositoryAndAdapterResponsibilities.
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

## Harness over the repository protocol

- [x] Implement the harness loop driving a `StageRepository`: claim eligible work-units, bounded concurrency, eligible-in-submission-order, retry-wait gating.
- [x] Implement graceful drain on termination signal (stop claiming, bounded drain timeout, none left running), cancellation (running ≤ bounded interval; queued-cancelled skipped), wall-clock timeout (→ `timed_out`, graceful-before-forceful, no orphan descendants), transient-resource cleanup on every outcome, and stale-unit recovery recording its cause.
- [x] Implement startup dependency validation gating work acceptance, structured logs with trace context, operational metrics, and `setproctitle` stage-role identification.
- [x] Build a stub `StageRepository` (in-memory) for harness tests.
- [x] Test `tests/pipeline/test_harness_adapter.py::test_new_stage_runs_via_adapter` and `::test_two_stores_share_harness`. **[R: harness over repository protocol]**
- [x] Test `tests/pipeline/test_harness_scheduling.py::test_concurrency_within_limit_in_order` and `::test_retry_wait_gates_eligibility` (wrap act with `no_task_leaks`). **[R: eligible-in-submission-order + bounded concurrency]**
- [x] Test `tests/pipeline/test_harness_drain.py::test_inflight_finishes_during_drain` and `::test_drain_timeout_enforced` (wrap with `no_task_leaks`/`no_thread_leaks`). **[R: graceful drain]**
- [x] Test `tests/pipeline/test_harness_cancel.py::test_running_cancelled_promptly` and `::test_queued_cancel_skipped`. **[R: cancellation]**
- [x] Test `tests/pipeline/test_harness_exec.py::test_timeout_recorded_no_orphans` and `::test_cleanup_on_every_outcome` (wrap with `no_thread_leaks`). **[R: bounded execution + cleanup]**
- [x] Test `tests/pipeline/test_harness_recovery.py::test_stale_unit_recovered_with_cause`. **[R: stale-unit recovery]**
- [x] Test `tests/pipeline/test_harness_startup.py::test_missing_dependency_blocks_acceptance`. **[R: startup validation gates acceptance]**
- [x] Test `tests/pipeline/test_harness_observability.py::test_lifecycle_logs_metrics_and_role`. **[R: lifecycle observability + stage-role]**

## Harness hardening (review round 2)

> Findings from a runtime-boundary review: process-global side effects, concurrency-guarantee wording, and partial-failure handling. Additive to the harness group; no `aizk.conversion` changes.

- [x] **(F1, blocking)** Signals reach only one harness: `install_signal_handlers` binds a process-wide handler to a single `ShutdownController` (and no-ops off the main thread), so two harnesses sharing a process cannot both observe SIGTERM/SIGINT — contradicting "two stages share a process."
  Add a process-level signal dispatcher that broadcasts shutdown to all registered controllers; test two controllers both observe one signal.
- [x] **(F1, doc)** Name the multi-harness semantics of the other process-global effects: `force_exit`/`os._exit` terminates the whole process (all harnesses); `setproctitle` is last-write in a shared process.
- [x] **(F2)** `test_concurrency_within_limit_in_order` asserts scheduler-dependent worker start order; assert the durable guarantee instead — claim/selection order (the first `limit` claimed are the first `limit` submitted) — and clarify in the harness docstring that "begin in submission order" means claim/dispatch order, not serialized thread starts (no start handshake).
- [x] **(F3, blocking)** Finalize DB failure strands completed work: on `OperationalError`/`DBAPIError` in `finalize` the harness pops the slot + cleans up while the unit stays `running` in the DB (delayed recovery + duplicate execution).
  Keep the slot (do not pop/cleanup) until the durable terminal transition lands so the reap loop retries it; regression test — finalize fails once then succeeds → the unit reaches its terminal status, not stranded.
- [x] **(F4)** Stub repo re-claims permanent failures: `finalize` clears `earliest_next_attempt_at` for permanent failures and `_ELIGIBLE_STATUSES` treats any FAILED as eligible.
  Persist retry class (or a distinct permanent-failed status) so only retryable-failed is eligible; test that a permanent failure is not re-claimed.

## Port conversion onto the primitives (behavior-preserving)

> This group is surgery, not file moves. The harness logic in `workers/` is interleaved with the conversion unit-of-work; carve the unit-of-work into the adapter **before** moving the loop. Sub-steps below capture the known hazards from the implementability review.

- [ ] **(carve-out, do first)** Extract the conversion unit-of-work from `orchestrator.py` (`process_job_supervised`, the upload phases / `UPLOAD_PENDING` transition, source-enrichment, `SubprocessMetadata` parsing) into `ConversionStageRepository.execute`/`finalize`, so the generic loop has a clean adapter to call.
  The orchestrator today is shell + unit-of-work fused; this is the substantial step.
- [ ] Implement `ConversionStageRepository` over `ConversionJob`, mapping conversion statuses onto the generic lifecycle: `UPLOAD_PENDING` stays a conversion-private status mapped to generic `running` (the harness never sees a "running sub-state"); conversion's timeout maps to generic `failed`/retryable (preserving today's `FAILED_RETRYABLE`-on-timeout behavior — conversion exercises only a subset of the generic terminal set); `NEW` remains never-committed.
  Keep owner-scoped idempotency, source-ref, upload phases, and output links in `aizk.conversion`.
- [ ] **(hazard F5)** Preserve the per-site `attempt` semantics and the `(aizk_uuid, attempt)` snapshot taken at supervision entry (API submit `attempt=0` with `from_status=None`; claim = post-increment; cancel = no-increment) when routing transitions through the generic helper, so retry-history scenarios stay green.
- [ ] Route conversion status transitions through the shared `record_transition` helper and relocate `conversion_job_events` into `pipeline_events` (Alembic migration), genericizing the typed columns per `design.md` § PipelineEventsGenericSchema.
- [ ] **(hazard F3)** Convert the module-global shutdown state (`workers/shutdown.py` `_shutdown_event`/`_signal_count`) into per-harness-instance state, preserving the `os._exit` force-exit-bypasses-pool-join behavior; signals stay process-wide but drain bookkeeping is per-instance (so two stages can share a process).
- [ ] Move the (now generic) harness loop out of `aizk.conversion.workers.{loop,orchestrator,shutdown,supervision}` into `aizk.pipeline`, delete the conversion-local duplicates, and wire the conversion worker entrypoint to run the shared harness with `ConversionStageRepository`.
- [ ] **(hazard F2)** Update `tests/.../test_no_direct_status_writes.py`: it asserts the only `.status = ConversionJobStatus.` write lives in `conversion/datamodel/events.py`; after the port, status writes flow through the generic helper and the `ConversionStageRepository`, so relocate/relax the guard to track the new sanctioned write path rather than failing the regression net.
- [ ] Verify the conversion suite is green unchanged (regression net): `uv run pytest -n auto -m "not integration_lifecycle" tests/conversion` and `uv run pytest -m integration_lifecycle tests/conversion`.
- [ ] Test `tests/conversion/.../test_event_relocation_equivalence.py`: after relocation, the events recorded for a conversion job match the pre-relocation event set (values), and the audit-survives-job-deletion behavior is preserved via `aizk_uuid`. **[behavior preservation]**

## Final spec reconcile (after the port is green)

- [ ] Relocate the now-duplicated generic contracts out of `worker-process-management` and `conversion-worker` into `pipeline-stage-runtime`: add MODIFIED/REMOVED deltas for the relocated requirements with `> Previously:` provenance, leaving the conversion specs holding only conversion-specific behavior.
  Update the change's delta specs accordingly.

## Documentation / decision record

- [ ] Add `src/aizk/pipeline/README.md` documenting the primitives (harness, repository protocol, run primitive, transition helper, lifecycle/state machine) and how a stage consumes them.
- [x] Record the primitives decision as an addendum to `docs/decision-record/009-orchestration.md` (brainstorming/design pass; may lag the specs).
