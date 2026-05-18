# Tasks: Conversion Job Event Log

## 1. Data Model and Payload Contract

- [x] Create new module `src/aizk/conversion/datamodel/events.py` with table-less imports only (`from __future__ import annotations`, datamodel imports).
- [x] Define `ConversionEventKind(str, Enum)` in `events.py` with the closed set: `queued`, `claimed`, `phase`, `cancelled`, `failed`, `succeeded`, `upload_pending`, `recovered_stale`, `source_enriched`.
- [x] Define one pydantic model per event kind in `events.py` (`QueuedPayload`, `ClaimedPayload`, `PhasePayload`, `CancelledPayload`, `FailedPayload`, `SucceededPayload`, `UploadPendingPayload`, `RecoveredStalePayload`, `SourceEnrichedPayload`) using fields enumerated in `design.md` § TypedDiscriminatedUnionPayload.
  Use `model_config = ConfigDict(extra="forbid")` on each variant for write-time strictness.
- [x] Provide a reader-side variant of each payload model that uses `extra="ignore"` (either via a separate model_config preset selected at deserialization time, or by reconstructing the union with the lenient config for read paths).
  Document the write-vs-read pattern in the module docstring.
- [x] Define `JobEventPayload = Annotated[Union[..., ...], Field(discriminator="kind")]` in `events.py` over all per-kind payload models.
- [x] Define `ConversionJobEvent(SQLModel, table=True)` in `events.py` with columns: `id` PK, `job_id` (FK → `conversion_jobs.id` with `ON DELETE SET NULL`, nullable), `aizk_uuid` (denormalized, indexed, non-null), `attempt` (int, non-null), `occurred_at` (timestamp, default `now()`), `kind` (enum string), `from_status` (nullable enum string), `to_status` (enum string), `payload_json` (Text).
  No `payload_version` column.
- [x] Add indexes `(job_id, occurred_at)`, `(aizk_uuid, occurred_at)`, `(kind, occurred_at)` to `ConversionJobEvent.__table_args__`.
- [x] Add unit tests `tests/conversion/unit/datamodel/test_event_payloads.py` that exercise the **full enumeration** of variants: a parametrized test runs valid-fields validation across every kind in `ConversionEventKind`, asserts unknown kind raises `ValidationError`, asserts an extra field on each known kind raises `ValidationError`, and asserts round-trip serialization preserves all fields per variant.
  Use `pytest.mark.parametrize` over `ConversionEventKind` so adding a new kind without test coverage is impossible.
- [x] Add unit test `test_reader_tolerates_additive_fields` simulating a previously-persisted row whose payload JSON contains a field not present in the current variant; assert the reader returns the recognized fields without raising.

## 2. Recording Helpers

- [x] Add `record_transition(session, job, *, to_status, kind, attempt, payload)` to `events.py`.
  Required behavior:
  - `attempt` is REQUIRED (no default) — callers must explicitly state which attempt the event belongs to per the table in `design.md` § HelperCallingConventions.
  - Read `from_status = job.status` _before_ mutation.
  - Validate `payload` against `JobEventPayload` (raises `ValidationError` on failure; helper does not catch).
  - Mutate `job.status = to_status`.
  - Construct a `ConversionJobEvent` with `from_status`, `to_status`, `kind`, `attempt`, `aizk_uuid = job.aizk_uuid`, `job_id = job.id`, serialized payload, and `occurred_at` defaulted by the model.
  - Call `session.add(job)` and `session.add(event)`.
  - **Do NOT call `session.commit()`** — the caller owns transaction boundaries.
- [x] Add `record_phase_event(session, *, job_id, aizk_uuid, attempt, current_status, phase, reported_at)` to `events.py` that builds a `PhasePayload` (validation raises on unrecognized phase or extra field), appends a `ConversionJobEvent` row with `from_status = to_status = current_status` (no projection mutation), and wraps the validation + `session.add` in a single `try / except` block that logs validation errors AND persistence errors at WARNING level and does not raise — best-effort persistence per requirement 2, fail-closed on validation per requirement 2.
- [x] Add `record_source_event(session, *, job_id, aizk_uuid, attempt, columns_written, update_succeeded, failure_reason)` to `events.py` that builds a `SourceEnrichedPayload` and appends a `ConversionJobEvent` row; same best-effort persistence as `record_phase_event`.
  Helper does not commit.
- [x] Module docstring on `events.py` documents: (1) caller owns commit, (2) `attempt` is explicit, (3) write-strict / read-lenient pydantic posture, (4) versioning via new `kind` variant for incompatible payload changes — reference `design.md` § TypedDiscriminatedUnionPayload.
- [x] Add unit tests `tests/conversion/unit/datamodel/test_event_helpers.py`.
  Tests use SQLite in-memory via `SQLModel.metadata.create_all(engine)` (no dependency on Alembic migrations).
  Cases:
  - `test_record_transition_does_not_commit` — assert the helper leaves an active transaction after returning; only the test's `session.commit()` makes the rows visible.
  - `test_record_transition_writes_both_rows_in_one_commit` — assert both rows commit atomically.
  - `test_record_transition_rolled_back_transaction_discards_both` — start a transaction, call the helper, roll back; assert neither row is observable in a fresh session.
  - `test_record_transition_validates_payload_before_mutation` — pass an invalid payload (extra field); assert `ValidationError` raises and `job.status` is unchanged.
  - `test_record_transition_attempt_is_required` — call without `attempt`; assert `TypeError`.
  - `test_record_phase_event_does_not_mutate_status` — assert projection unchanged.
  - `test_record_phase_event_persistence_failure_is_swallowed_and_logged` — monkey-patch session to raise on `add`; assert no exception propagates and a log record exists.
  - `test_record_phase_event_validation_failure_drops_row` — pass an unrecognized phase string; assert no row inserted and a log record exists, no exception propagates.

## 3. Database Migration

- [x] Generate new Alembic migration `src/aizk/conversion/migrations/versions/<rev>_add_conversion_job_events.py` creating the `conversion_job_events` table with all columns and indexes from task group 1, including the FK `job_id REFERENCES conversion_jobs(id) ON DELETE SET NULL`.
- [x] Verify the migration is reversible (`downgrade()` drops the table).
- [x] Add integration test `tests/conversion/integration/test_migrations.py::test_conversion_job_events_table_created` asserting the table and indexes exist after `alembic upgrade head` against a fresh SQLite database.
- [x] Add integration test `test_conversion_job_events_job_id_fk_set_null_on_delete` that inserts a job + event row, deletes the job, and asserts the event row's `job_id` is NULL and `aizk_uuid` remains populated.
- [x] Add integration test for migration downgrade — assert table is dropped after `alembic downgrade -1`.

## 4. Worker Write-Site Migration

Per-site `attempt` value follows `design.md` § HelperCallingConventions.

- [x] Rewrite [loop.py:100](src/aizk/conversion/workers/loop.py#L100) (`claim_next_job`) to call `record_transition(session, job, to_status=RUNNING, kind="claimed", attempt=job.attempts, payload=ClaimedPayload(...))` AFTER incrementing `job.attempts`.
- [x] Rewrite [loop.py:51](src/aizk/conversion/workers/loop.py#L51) (`recover_stale_running_jobs`) to call `record_transition(session, job, to_status=FAILED_RETRYABLE, kind="recovered_stale", attempt=job.attempts, payload=RecoveredStalePayload(stale_after_minutes=..., last_started_at=job.started_at))`.
- [x] Rewrite [orchestrator.py:170](src/aizk/conversion/workers/orchestrator.py#L170) (`_initialize_running_job` re-entrant claim) to call `record_transition` with `kind="claimed"` and `attempt=job.attempts` after the conditional increment.
  Document inline that this is the re-entrant claim path distinct from `claim_next_job`.
- [x] Rewrite [orchestrator.py:493](src/aizk/conversion/workers/orchestrator.py#L493) to call `record_transition(session, job, to_status=UPLOAD_PENDING, kind="upload_pending", attempt=job.attempts, payload=UploadPendingPayload(content_hash=...))`.
- [x] Rewrite [orchestrator.py:611,614](src/aizk/conversion/workers/orchestrator.py#L611) (`handle_job_error` retryable + permanent arms) to call `record_transition` with `kind="failed"`, `attempt=job.attempts`, and `FailedPayload(error_code=..., error_message=message, error_detail=error_detail, retryable=..., last_phase=...)` carrying the _post-sanitization_ message and detail.
- [x] Rewrite [uploader.py:341](src/aizk/conversion/workers/uploader.py#L341) to call `record_transition(session, job, to_status=SUCCEEDED, kind="succeeded", attempt=job.attempts, payload=SucceededPayload(output_id=..., content_hash=...))`.
- [x] Wire phase-event drain in [supervision.py](src/aizk/conversion/workers/supervision.py): when a `phase` event is drained from the subprocess `status_queue`, call `record_phase_event` with the current `(job_id, aizk_uuid, attempt)` snapshot.
  The snapshot is taken in the parent process at the moment the subprocess is spawned, not from the subprocess metadata (which has no access to the job's attempt count).
- [x] Wire `record_source_event` call in [orchestrator.py:154](src/aizk/conversion/workers/orchestrator.py#L154) (`_write_source_enrichment`) to fire after the Source UPDATE attempt with `update_succeeded` reflecting outcome, `failure_reason` populated on the exception path, and `attempt=job.attempts`.

Integration tests under `tests/conversion/integration/test_job_event_log.py`:

- [x] `test_successful_job_emits_full_event_sequence` runs a real conversion end-to-end and asserts the event log contains `queued (attempt=0) → claimed (attempt=1) → phase preparing_input (attempt=1) → phase converting (attempt=1) → upload_pending (attempt=1) → succeeded (attempt=1)` in order with correct payloads, and asserts the multi-phase-per-attempt invariant (at least two `phase` events for the same attempt).
- [x] `test_retryable_failure_preserves_prior_attempt_events` runs a job that fails attempt 1 (retryable) then succeeds on attempt 2; assert both attempts' events remain in the log with distinct `attempt` values, the attempt-1 event has `kind = "failed"` (not `"recovered_stale"`), and the attempt-2 events follow with `attempt = 2`.
- [x] `test_permanent_failure_arm_emits_failed_event_with_non_retryable_indicator` runs a job that hits a non-retryable error (e.g., `NoConverterForFormat` or empty content); assert one `failed` event with `to_status = FAILED_PERM` and `retryable = false` in the payload.
- [x] `test_egress_policy_error_does_not_persist_destination` injects an `EgressPolicyError` from the subprocess; assert the persisted `failed` event payload's `error_message` equals the error_code and `error_detail` is null (sanitization preserved).
- [x] `test_stale_recovery_uses_recovered_stale_kind` sets a job to RUNNING with stale `started_at`, runs `recover_stale_running_jobs`, asserts one `recovered_stale` event with the threshold and prior timestamp in payload.
- [x] `test_initialize_running_job_reentrant_path_emits_claimed_event` simulates a job already in RUNNING (e.g., from a crashed prior worker that didn't get caught by stale-recovery) and exercises `_initialize_running_job`; assert the re-entrant claim path emits a `claimed` event with the incremented attempt.
- [x] `test_transition_rollback_leaves_job_in_prior_status` starts a transition transaction, injects a constraint violation between mutation and commit, rolls back; assert `ConversionJob.status` is observably unchanged in a fresh session and no event row exists.
- [x] `test_subprocess_terminal_events_not_persisted_via_subprocess_channel` injects subprocess `failed` and `cancelled` events; assert the event log contains only the orchestrator-authored transition event, not a subprocess-sourced row.
- [x] `test_phase_event_persistence_failure_does_not_halt_job` monkey-patches the events session to raise on phase-event insert; assert the job still reaches SUCCEEDED and the failure is logged.
- [x] `test_phase_event_with_unrecognized_phase_is_dropped` injects a `phase` report with an unrecognized phase string into the subprocess channel; assert no event row is inserted, the validation failure is logged, and the job continues to SUCCEEDED.
- [x] `test_source_enriched_event_emitted_on_success_and_failure` runs one job where the Source UPDATE succeeds and one where it fails; assert one `source_enriched` event per job carrying the correct `update_succeeded` flag.
- [x] `test_direct_source_mutation_emits_no_event` directly mutates a `Source` row via `session.add(source)` outside `_write_source_enrichment`; assert no `source_enriched` event is appended.

## 5. API Write-Site Migration

- [x] Rewrite [jobs.py:264](src/aizk/conversion/api/routes/jobs.py#L264) (job submission) to construct the ConversionJob with `status=QUEUED`, call `session.add(job); session.flush()` so the event row can capture `job_id`, then call `record_transition(session, job, to_status=QUEUED, kind="queued", attempt=0, payload=QueuedPayload(submitted_by=principal.subject, requeue_reason="initial"), from_status=None)`.
  The explicit `from_status=None` produces the origin event's NULL prior status per spec R1 "Initial submission event has no prior status"; without it the helper would derive QUEUED from the just-constructed row.
  See `design.md § HelperCallingConventions` for the per-site table.
- [x] Rewrite [jobs.py:117](src/aizk/conversion/api/routes/jobs.py#L117) (`_apply_job_retry`) to call `record_transition` with `kind="queued"`, `attempt=job.attempts` AFTER the increment at `job.attempts += 1`, and `QueuedPayload(requeue_reason="retry_endpoint", submitted_by=principal.subject)`.
  The current direct status mutation in `_apply_job_retry` becomes the helper call.
- [x] Rewrite [jobs.py:136](src/aizk/conversion/api/routes/jobs.py#L136) (`_apply_job_cancel`) to call `record_transition` with `kind="cancelled"`, `attempt=job.attempts`, and `CancelledPayload(cancelled_by=principal.subject, cancellation_reason=...)`.
- [x] Add contract test `tests/conversion/contract/test_jobs_event_log.py::test_job_submission_emits_queued_event` asserting POST /jobs commits both the job row and exactly one `queued` event (`from_status = NULL`, `attempt = 0`) in one transaction.
- [x] Add contract test `test_retry_endpoint_emits_queued_event_with_retry_reason` asserting POST /jobs/{id}/retry produces a `queued` event whose payload `requeue_reason = "retry_endpoint"` and `attempt` equals the incremented attempt value.
- [x] Add contract test `test_cancel_endpoint_emits_cancelled_event` asserting POST /jobs/{id}/cancel produces a `cancelled` event with the API user as `cancelled_by`.
- [x] Add contract test `test_bulk_retry_emits_one_queued_event_per_job` asserting POST /jobs/actions with retry action across N jobs produces N `queued` events in one transaction (or N transactions, whatever the route's implementation), one per affected job.
- [x] Add contract test `test_bulk_cancel_emits_one_cancelled_event_per_job` asserting the parallel for cancel.

## 6. Append-Only Enforcement and Direct-Status-Write Lint Guard

- [x] Add regression test `tests/conversion/unit/test_no_direct_status_writes.py` that scans `src/aizk/conversion/` for occurrences of the pattern `\.status\s*=\s*ConversionJobStatus\.` (regex), and asserts every match lives inside `src/aizk/conversion/datamodel/events.py`.
  Use `pathlib` glob + `re` against text — no shell, no `ast`.
- [x] Add regression test `tests/conversion/unit/test_event_log_is_append_only.py` that scans `src/aizk/conversion/` for occurrences of patterns that would UPDATE or DELETE the event log table: `session.delete(\w*event)`, `UPDATE\s+conversion_job_events`, `DELETE\s+FROM\s+conversion_job_events`.
  Assert no matches outside of test fixtures and the Alembic migration's `downgrade()` function.
- [x] Verify both lint guards fail when a fake direct-status assignment or event UPDATE is added in another file (manual sanity check during PR review).

## 7. Documentation and Sync

- [x] Update `src/aizk/conversion/datamodel/events.py` module docstring to cover: (1) helper-calling conventions (caller commits, explicit `attempt`), (2) write-vs-read pydantic stance (`extra="forbid"` on write, `extra="ignore"` on read), (3) versioning rule (incompatible changes → new `kind` variant; additive changes → tolerated by read leniency).
  Reference the relevant sections of `.specs/changes/2026-05-17-conversion-job-event-log/design.md`.
- [x] Run `sdd-sync` after implementation completes to merge the delta into the baseline `.specs/specs/conversion-worker/spec.md`.

## 8. Follow-Up (out of scope, tracked here so they don't fall off)

- [ ] Future change: event-table retention policy.
  Trigger SHALL be **row count exceeds N OR table size exceeds M megabytes, whichever comes first**, with N and M chosen at design time for that change.
  Mechanism likely a TTL by `occurred_at` with terminal-event records retained longer than progress-event records; concrete policy is deferred.
