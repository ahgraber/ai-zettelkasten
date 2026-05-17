# Design: Conversion Job Event Log

## Context

The conversion pipeline currently persists only the _current_ state of each `ConversionJob` on a single mutable row.
Retries overwrite the previous attempt's `error_code`, `error_message`, and `last_error_at`; the subprocess emits structured `phase` / `completed` / `failed` / `cancelled` events on an `mp.Queue` that the parent drains and discards; `Source` row enrichment is mutated in place without recording which job authored the values.

Status mutations are scattered across at least ten sites: API submit, API cancel, API retry-requeue, worker claim, worker stale-recovery sweep, orchestrator `handle_job_error` (retryable + permanent arms), orchestrator UPLOAD_PENDING transition, uploader SUCCEEDED, and `_initialize_running_job`.
Each site writes `job.status = ...` and commits.

Constraints shaping this design:

- SQLite is the database.
  WAL mode + `synchronous=NORMAL` are already in use ([conversion-worker spec § Technical Notes](../../specs/conversion-worker/spec.md)).
  Adding a second row insert per status mutation must stay inside the existing transaction so the projection and the log can never diverge across a committed boundary.
- The orchestrator's `handle_job_error` sanitizes egress-policy errors at [orchestrator.py:587-590](../../../src/aizk/conversion/workers/orchestrator.py#L587) — replacing the message with just the error code and dropping the traceback — to prevent rejected destinations from landing in durable storage.
  Any new persistence path must not undo this.
- The API and the worker both already import from `aizk.conversion.datamodel`.
  Locating the event log infrastructure there keeps the existing module dependency graph intact.

## Decisions

### Decision: AppendOnlyEventLog

**Chosen:** Add a `conversion_job_events` table whose rows are written in the same transaction as the `ConversionJob.status` mutation that produced them.
The `ConversionJob` row remains the authoritative current-state projection; the event table is observability and audit.
No projector, no state rebuild, no snapshots.

The table denormalizes `aizk_uuid` from `conversion_jobs.aizk_uuid` onto each event row.
This is not strictly required for finding events of a given job (the `job_id` FK would suffice), but it serves two purposes: (1) future processing-stage event tables that share the same Source identity can be queried alongside `conversion_job_events` by `aizk_uuid` without joining through `conversion_jobs`, and (2) it keeps the audit trail queryable after a job row is deleted (see below).

The `job_id` FK SHALL use `ON DELETE SET NULL`.
The conversion API today permits deletion of jobs in terminal states (FAILED_RETRYABLE, FAILED_PERM, CANCELLED) via `_apply_job_delete` ([jobs.py:142](../../../src/aizk/conversion/api/routes/jobs.py#L142)).
A naive FK without `ON DELETE` would either CASCADE the audit (destroying replay history on every operator deletion) or RESTRICT the delete endpoint (breaking the existing API contract).
`SET NULL` preserves the event records — their `aizk_uuid` is still populated, so the audit remains queryable by Source identity — while permitting job deletion to proceed.
The `job_id` column on the event row goes to NULL once the job is deleted; readers that need to follow job-specific history for non-deleted jobs use the FK, and readers that need full lifecycle history (including post-deletion) use `aizk_uuid`.

**Rationale:** The wins this change targets — per-attempt failure visibility, phase-at-timeout, identity-row provenance — are achievable without the projection/eventual-consistency cost of full event sourcing.
Keeping `ConversionJob.status` as the source of truth means the worker loop, the API list/get endpoints, and the readiness probes need no rewiring.
Same-transaction insert is what eliminates the "log says SUCCEEDED but row says FAILED_RETRYABLE" failure mode that an outbox or async publisher would re-introduce.

**Alternatives considered:**

- **Full event sourcing.**
  Replace `ConversionJob.status` with a projection rebuilt from events.
  Heavy: schema versioning, snapshot management, eventual-consistency reasoning.
  Buys little for a single-tenant pipeline where one process owns its read model.
- **Structured logging only (no DB table).**
  Cheaper to write, but querying "all attempts of job 42" requires log-store gymnastics.
  The DB is already authoritative for job identity; co-locating the audit there matches operator workflow.
- **Outbox pattern.**
  Same DB write, plus an async fan-out to consumers.
  Unused — no downstream consumers today.
  Adds complexity that earns nothing.

### Decision: SharedHelperInDatamodelLayer

**Chosen:** Locate `record_transition(session, job, *, to_status, kind, attempt, payload)` in `aizk.conversion.datamodel.events` (new module).
Both API routes and worker code import it from there.
The helper is the only sanctioned path to mutate `ConversionJob.status`.

**Rationale:** Both the API and the worker already import `ConversionJob` from `aizk.conversion.datamodel.job`.
Placing the helper alongside the entity it mutates matches the existing dependency direction (callers → datamodel) and avoids importing worker modules from the API.
It also means the contract — "every status mutation co-commits an event" — is enforced by a single function rather than by reviewer discipline across ten call sites.

**Alternatives considered:**

- **Two helpers — one in API, one in worker.**
  Symmetric but duplicative.
  Drift between the two implementations is the failure mode this change is supposed to prevent in the first place.
- **SQLAlchemy event listener on `ConversionJob.status` change.**
  Magical: any `session.add(job)` would trigger an event row.
  Fragile under bulk updates, hard to test, and the listener can't easily access the structured payload the caller has in hand.
- **Require callers to write both rows manually.**
  Cheap but every new transition site must remember the second write.
  Identical to the failure mode in the current scattered-mutation state.

### Decision: HelperCallingConventions

**Chosen:** The helper API has two non-obvious calling conventions that affect correctness:

1. **The helper does not commit.**
   It calls `session.add(job)` and `session.add(event)` and returns.
   The caller owns transaction boundaries.
2. **`attempt` is an explicit required parameter, not inferred from `job.attempts`.**
   The caller passes the attempt number that the new event belongs to.

**Rationale for "does not commit":** Callers run inside heterogeneous transaction shapes that already determine commit boundaries.
The API submit path at [jobs.py:223-273](../../../src/aizk/conversion/api/routes/jobs.py#L223) opens an explicit `BEGIN IMMEDIATE` and runs idempotency check + queue-depth check before its final commit; a helper that committed internally would break queue-depth integrity by closing the transaction mid-check.
The worker's `claim_next_job` also runs in an explicit `BEGIN IMMEDIATE` block ([loop.py:73-105](../../../src/aizk/conversion/workers/loop.py#L73)).
The orchestrator's `handle_job_error` runs in the session's implicit transaction.
Forcing the helper to commit would either be wrong for the API path or would require a "do you want me to commit?"
flag — which is the same as not committing and letting callers do it.

**Rationale for explicit `attempt`:** The `attempt` field on an event identifies "the attempt this transition belongs to."
For some sites that is `job.attempts` _before_ the helper is called (a `failed` event for the attempt that just ran); for others it is `job.attempts + 1` _after_ the caller has already incremented (a `claimed` event for the attempt about to start).
`claim_next_job` increments `job.attempts` before calling the helper, so passing `job.attempts` would give the right answer there — but `_apply_job_retry` also increments `attempts` ([jobs.py:116](../../../src/aizk/conversion/api/routes/jobs.py#L116)) while `_apply_job_cancel` does not.
Making the helper infer from `job.attempts` therefore couples the helper to whether the caller has incremented yet.
An explicit parameter forces each call site to think about which attempt the event belongs to, and the answer becomes part of code review rather than a hidden invariant.

**Per-site `attempt` values** (documented for implementers):

| Caller                                            | `attempt` value                                                                                 |
| ------------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| API submit (`NEW → QUEUED`)                       | `0` (the QUEUED event precedes any attempt)                                                     |
| API retry (`FAILED_* → QUEUED`)                   | `job.attempts` after increment (the new pending attempt)                                        |
| API cancel                                        | `job.attempts` (the cancellation belongs to the in-flight attempt if any, else the most recent) |
| Worker claim (`QUEUED → RUNNING`)                 | `job.attempts` after increment (the new attempt about to run)                                   |
| Worker stale recovery                             | `job.attempts` (the failed attempt the sweep is recovering)                                     |
| Orchestrator `_initialize_running_job` re-entrant | `job.attempts` after increment if a new attempt started, else the current value                 |
| Orchestrator UPLOAD_PENDING                       | `job.attempts` (same attempt as the just-completed RUNNING)                                     |
| Orchestrator failure handler                      | `job.attempts` (the attempt that failed)                                                        |
| Uploader SUCCEEDED                                | `job.attempts` (same attempt as UPLOAD_PENDING)                                                 |
| Source enrichment                                 | `job.attempts` (the attempt that produced the metadata)                                         |

**Alternatives considered:**

- **Helper commits internally.**
  Breaks the API submit path's BEGIN IMMEDIATE block.
  Rejected.
- **Helper infers `attempt` from `job.attempts`.**
  Off-by-one at every caller that doesn't match the inferred convention (e.g., API submit, stale recovery, cancellation).
  The required convention shifts the bug from "off-by-one in the helper" to "off-by-one at the caller" — equally bad and harder to debug because the helper looks correct.
  Rejected.

### Decision: PassiveLogNotStateMachineValidator

**Chosen:** `record_transition` writes whatever `(from_status, to_status)` pair the caller supplies.
It does not validate against a legal-transitions table at runtime.

**Rationale:** The change is scoped to "stop losing diagnostic information."
Adding runtime state-machine enforcement is a separate, larger discussion about which transitions are legal (e.g., is `FAILED_PERM → QUEUED` valid?
today the retry endpoint allows it).
Bundling enforcement into the logging change couples two questions that should be answered independently.
The legal-transitions matrix is documented below as a reference but not enforced in code.

**Alternatives considered:**

- **Strict state-machine enforcement.**
  Runtime rejection of illegal pairs.
  Catches transition bugs at write time.
  Out of scope; reconsider after the log has been live for a release and we know what drift looks like.
- **Whitelist as a warning, not an error.**
  Logs WARN on illegal pairs but writes anyway.
  Adds runtime branching without forcing a fix.
  Worst-of-both.

### Decision: PhaseEventsOnlyFromSubprocessChannel

**Chosen:** Persist only subprocess-reported `phase` events to the log.
Subprocess `completed` / `failed` / `cancelled` events continue to drive parent control flow but produce no log row of their own.
The terminal record is always the orchestrator-authored transition event committed via `record_transition`.

**Rationale:** The orchestrator's `handle_job_error` sanitizes egress-policy errors before the message touches durable storage — replacing the message with the bare error code and dropping the traceback.
A second writer that persists the raw subprocess `failed` event would undo this sanitization on every egress-policy rejection (and any future error class that gains similar sanitization).
Constraining persistence to the single sanitized writer is a defense-in-depth contract guarantee: no future contributor can re-introduce destination leakage by accident, because the subprocess channel simply isn't a persistence input.

**Alternatives considered:**

- **Persist subprocess terminal events with `traceback` stripped.**
  Preserves the pre-sanitization `error_code` and `retryable` flag, at the cost of duplicating sanitization logic in two writers and forcing every future sanitization rule to be applied in both places.
  The marginal diagnostic value (subprocess's raw `error_code` vs orchestrator's interpreted `error_code`) does not justify the duplicated-logic risk.
- **Persist subprocess terminal events fully, sanitize at insertion.**
  Same logic duplication, plus the sanitization rule becomes a property of the persistence layer rather than the error-handling layer.
  Wrong locality.
- **Persist subprocess terminal events fully, no sanitization.**
  Reintroduces the egress-policy bypass.
  Rejected.

### Decision: TypedDiscriminatedUnionPayload

**Chosen:** Define `JobEventPayload` as a discriminated union (pydantic `Field(discriminator="kind")`) with one variant per event kind.
The `payload_json` column stores the serialized variant.
Insertion validates the payload via pydantic with `model_config = ConfigDict(extra="forbid")`; reads use `extra="ignore"` so forward-compatible additive fields are tolerated.

There is **no `payload_version` column.**
The `kind` enum carries version identity: an incompatible payload change to an existing kind (renamed field, removed field, changed type) SHALL be expressed by introducing a new kind to the closed enumeration (e.g., `failed` → `failed_v2`) rather than mutating the existing kind's contract.
Old rows persist with their original kind and deserialize using the original variant; new code writes the new kind.
Additive payload changes (new optional fields on an existing kind) do not need a new kind because `extra="ignore"` on read makes old readers tolerate the new fields.

**Initial variants:**

```text
QueuedPayload(submitted_by: str | None, requeue_reason: Literal["initial", "retry_endpoint"])
ClaimedPayload(claimed_at: datetime, worker_pid: int | None)
PhasePayload(phase: Literal["preparing_input", "converting", "uploading"], reported_at: datetime)
CancelledPayload(cancelled_by: str | None, cancellation_reason: str | None)
FailedPayload(error_code: str, error_message: str, error_detail: str | None, retryable: bool, last_phase: str | None)
SucceededPayload(output_id: int, content_hash: str)
UploadPendingPayload(content_hash: str)
RecoveredStalePayload(stale_after_minutes: int, last_started_at: datetime)
SourceEnrichedPayload(aizk_uuid: UUID, columns_written: list[str], update_succeeded: bool, failure_reason: str | None)
```

`error_message` on `FailedPayload` carries the post-sanitization message (the same value that lands in `ConversionJob.error_message`). `error_detail` carries the post-sanitization detail (already `None` for egress-policy errors).

**Rationale:** Matches the project's existing typed-IPC posture ([SubprocessMetadata](../../../src/aizk/conversion/workers/types.py) uses the same approach: pydantic at the boundary, rejection at write time).
Discriminated unions give us closed-set guarantees: an unknown `kind` or an extra field for a known `kind` raises `ValidationError` at insertion, surfacing schema drift loudly.
Per-kind classes mean payload-field changes are typed refactors, not silent dict-key renames.

The "kind enum carries version" approach (rather than a `payload_version` column) is appropriate because event payloads are leaf-shaped (3–6 fields each) and the expected change pattern is "rename a field in one payload," not "coordinated reshape across multiple blocks" (the manifest case, which uses a version field — `ManifestV1` / `ManifestV2` — because v1.0 → v2.0 changed multiple blocks at once).
For leaf-grained changes, a new kind is more localized than a global version bump.

**Alternatives considered:**

- **Add `payload_version` column with reader dispatch.**
  Matches the manifest pattern but introduces parametric `(version, kind) → model` dispatch for every reader.
  The proposal explicitly scoped schema versioning OUT; adopting the manifest pattern here would have required scoping it back IN.
  Rejected for current grain.
- **Free-form `payload_json: dict`.**
  Lighter; matches "best-effort structured logging" framing.
  Loses the closed-set guarantee — a typo in a kind name or field name persists silently and only surfaces when a reader fails.
  The project already pays the discriminated-union cost elsewhere; consistency wins.
- **Per-kind table per variant.**
  Maximally type-safe at the DB level but explodes the schema.
  Querying "all events for job X across kinds" becomes a UNION across many tables.
  Not worth it for a log table.

### Decision: SourceEnrichmentEventViaSiblingHelper

**Chosen:** Add a sibling helper `record_source_event(session, *, job_id, attempt, aizk_uuid, columns_written, update_succeeded, failure_reason)` in the same `datamodel.events` module.
`_write_source_enrichment` calls it after the Source UPDATE attempt (whether the UPDATE succeeded or failed).
The helper appends one `source_enriched` event row.

The Source UPDATE and the event-row insert do _not_ share a transaction: Source enrichment is already best-effort (`_write_source_enrichment` swallows all exceptions and logs), and forcing a shared transaction would invert that posture.
The audit record's purpose is "what did the worker attempt and what was the outcome," not "the Source UPDATE atomically committed alongside the audit."
If the event-row insert itself fails, the failure is logged and the conversion job proceeds — same lenience as the phase-event persistence stance.

**Rationale:** A separate helper signals that the contract is different: `record_transition` mutates the ConversionJob row in the same transaction as the event; `record_source_event` does not mutate (the Source mutation happens upstream) and does not require the same atomicity.
Reusing `record_transition` would conflate two different contracts and force a `to_status` that doesn't apply.

**Alternatives considered:**

- **Reuse `record_transition` with `to_status=None`.**
  Conflates the contracts.
  The "from_status" / "to_status" columns are a structural answer to the question "what status did this row become?"
  and forcing them to NULL for non-transition events erodes the column's meaning.
- **Share a transaction with the Source UPDATE.**
  Tightens the contract beyond what Source enrichment offers today.
  The existing requirement explicitly states Source enrichment is best-effort and "the manifest's authoritative values are unaffected."
  Promoting it to transactional would expand scope.

## Architecture

```text
                    +-----------------------------+
                    |  aizk.conversion.datamodel  |
                    |   .events  (NEW MODULE)     |
                    |                             |
                    |  record_transition()        |
                    |  record_phase_event()       |
                    |  record_source_event()      |
                    |  JobEventPayload (union)    |
                    +--------------+--------------+
                                   ^
            +----------------------|----------------------+
            |                      |                      |
   +--------+--------+    +--------+--------+    +--------+--------+
   |  API routes     |    |  Worker loop    |    | Orchestrator    |
   |                 |    |                 |    | / Uploader      |
   |  submit  →      |    |  claim_next_job |    |                 |
   |    record_transition|  → record_      |    | handle_job_error|
   |    (NEW→QUEUED) |    |    transition   |    |  → record_      |
   |                 |    |    (QUEUED→     |    |    transition   |
   |  cancel  →      |    |     RUNNING)    |    |    (RUNNING→    |
   |    record_transition|                  |    |     FAILED_*)   |
   |    (*→CANCELLED)|    |  recover_stale_ |    |                 |
   |                 |    |    running_jobs |    | _initialize_    |
   |  retry   →      |    |  → record_      |    |   running_job   |
   |    record_transition|    transition    |    |  → record_      |
   |    (FAILED_*→   |    |    (RUNNING→    |    |    transition   |
   |     QUEUED)     |    |     FAILED_     |    |                 |
   |                 |    |     RETRYABLE,  |    | uploader        |
   +--------+--------+    |     recovered_  |    |  success path   |
            |             |     stale)      |    |  → record_      |
            |             +--------+--------+    |    transition   |
            |                      |             |    (UPLOAD_     |
            |                      |             |    PENDING→     |
            |                      |             |    SUCCEEDED)   |
            |                      |             +--------+--------+
            |                      |                      |
            v                      v                      v
   +-----------------------------------------------------------------+
   |                       SQLite (WAL)                              |
   |                                                                 |
   |  conversion_jobs              conversion_job_events             |
   |  (status mutation)            (append-only)                     |
   |       \____________ same txn ____________/                      |
   +-----------------------------------------------------------------+

                Parent worker supervision loop
                            |
                            v
                    +---------------+
                    | mp.Queue from |
                    | subprocess    |
                    +-------+-------+
                            |
                  +---------+---------+
                  |                   |
                  v                   v
              kind=phase     kind ∈ {completed,
                  |          failed, cancelled}
                  |                   |
                  v                   v
       record_phase_event()      drives parent
       (best-effort, no          control flow only;
        status mutation)         orchestrator emits
                  |              the transition event
                  v              via record_transition
       conversion_job_events
       (insert one row)
```

Key invariants enforced by structure:

- **Single writer per terminal outcome.**
  Every `failed` / `succeeded` / `cancelled` row in `conversion_job_events` comes from one of three orchestrator/API code paths, all funneled through `record_transition`.
  The subprocess channel cannot author terminal events.
- **Co-committed projection and log.**
  `record_transition` performs the `ConversionJob.status` mutation and the event-row INSERT in the same `Session.commit()`.
  Rollback discards both.
- **Sanitization locality.**
  Egress-policy sanitization lives in `handle_job_error` (one place, unchanged).
  Because subprocess terminal events do not persist, no second sanitization point is needed.

## Risks

- **Missed transition sites cause silent log gaps.**
  A new code path that mutates `ConversionJob.status` directly (bypassing `record_transition`) would commit a status change with no event row.
  Mitigation: lint/test pass that fails CI if `job.status = ConversionJobStatus.*` appears outside `aizk.conversion.datamodel.events`.
  Add a regression test that greps for direct status assignment in `src/aizk/conversion/` and asserts the only matches are inside the events module.
- **Doubled write volume on every status mutation.**
  Each transition now writes two rows in one transaction instead of one.
  SQLite WAL handles this fine in current volumes; flag for revisit if write throughput becomes a constraint.
  No new index hot spots — both indexes (`(job_id, occurred_at)`, `(kind, occurred_at)`) are append-friendly.
- **Phase-event write amplification under high concurrency.**
  Every phase report (typically 3-4 per attempt) now hits the DB.
  With `worker_concurrency` ≥ 4 and busy GPU jobs, this could create lock contention.
  Mitigation: phase-event persistence is best-effort per requirement 2 — under contention, failures are logged and skipped.
  Monitor a counter for phase-event-write failures; if it climbs, batch within an attempt or downgrade to log-only.
- **Payload-schema drift between writers and readers.**
  A future contributor renames a field in `FailedPayload` (an incompatible change) without introducing a new kind.
  Writers using `extra="forbid"` will fail at insertion as soon as the new code ships, which is loud — good.
  But if the contributor renames the field both in the writer and in the reader model in a single PR, old persisted rows (with the original field name) become unreadable by the new reader.
  Mitigation: enforce that incompatible payload changes introduce a new kind variant (e.g., `failed` → `failed_v2`), not in-place edits to an existing variant.
  The closed-enum lint + code review surfaces this; the `kind` enum itself is the audit trail.
  Additive changes are fine because readers use `extra="ignore"`.
- **`ConversionJob.id` is a 4-byte int.**
  The event-table FK is also `int`.
  At current job volumes the int never wraps, but the event log will grow much faster than the job table (multiple rows per job).
  Plan an event-table retention policy before the event table dominates DB size.
  The trigger SHALL be "row count exceeds N OR table size exceeds M megabytes, whichever comes first," with N and M chosen at the time the policy is designed.
  Out of scope for this change; flagged in tasks group 8.

## Verification Waivers

None.
Every SHALL requirement is testable via integration tests against the real SQLite database (the project's `integration_lifecycle` and standard integration suites already exercise the worker against SQLite).
The egress-policy-sanitization scenario is exercised by injecting an `EgressPolicyError` from the subprocess and asserting the persisted event payload contains no destination.
