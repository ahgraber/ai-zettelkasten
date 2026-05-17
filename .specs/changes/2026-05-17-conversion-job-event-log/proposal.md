# Proposal: Conversion Job Event Log

## Intent

The ingestion pipeline carries ConversionJob through a multi-stage lifecycle (`NEW → QUEUED → RUNNING → UPLOAD_PENDING → SUCCEEDED`, plus `FAILED_RETRYABLE` / `FAILED_PERM` / `CANCELLED` branches) but persists only the _current_ state on a single mutable row.
Retries overwrite the previous attempt's `error_code` and `error_message`, the subprocess-emitted `phase` / `completed` / `failed` events are dropped after the parent reads them, and `Source` enrichment writes leave no record of which job authored the values.
This loses information that is already structured and that operators currently have no way to reconstruct: per-attempt failure reasons, the phase a timed-out job was last in, and the provenance of identity-row writes.

The goal is to add an append-only event log that captures every transition and every subprocess-reported phase event as a durable record, _without_ replacing the existing `ConversionJob.status` projection.
The current-state row stays the source of truth for scheduling and read paths; the log is observability and audit.

## Scope

**In scope:**

- New `conversion_job_events` SQLModel table (append-only) with `job_id`, `aizk_uuid` (denormalized), `attempt`, `occurred_at`, `kind`, `from_status`, `to_status`, and a typed `payload_json` column.
- A typed event-payload model — pydantic discriminated union keyed by `kind` — matching the existing `SubprocessMetadata` posture so payload-schema drift fails loudly.
- A single `record_transition(...)` helper that every status-mutating site in `conversion-worker` and the API routes funnels through.
  Replaces the ~10 scattered `job.status = ...` writes.
- Persistence of subprocess-emitted `phase` events that today arrive on the parent's `mp.Queue` and are then discarded.
  Subprocess-emitted `completed` / `failed` / `cancelled` events remain real-time control signals and are NOT persisted; the orchestrator's transition event (same-transaction with the status mutation, sanitized for egress-policy errors) is the canonical terminal record.
- A `source_enriched` event recording every `_write_source_enrichment` write with the originating `job_id` and the column set written.
- Alembic migration creating the new table and its indexes.

**Out of scope:**

- Any read API for events (no `GET /jobs/{id}/events`).
  Events are queryable via SQL and structured logs only.
- Removing or weakening `ConversionJob.status` / `attempts` / `error_*` fields — the existing row remains the current-state projection.
- Rebuilding state by replaying events.
  No projector, no snapshots, no eventual-consistency semantics.
- Outbox / event bus / pub-sub.
  Events are written in the same transaction as the status mutation; no fan-out.
- A `payload_version` column or any reader-side version-dispatch machinery.
  Versioning is implicit in the closed `kind` enum: incompatible payload changes (renamed/removed fields, changed types) introduce a new kind variant (e.g., `failed` → `failed_v2`); additive changes are tolerated by `extra="ignore"` on read.
- Changes to S3 manifest contents — the manifest is already an immutable per-job artifact and remains the canonical replay record.

## Approach

A new `conversion_job_events` table sits alongside `conversion_jobs`.
Every site that mutates `ConversionJob.status` is rewritten to call `record_transition(session, job, *, to_status, kind, attempt, payload)`, which (a) mutates `job.status` exactly as today and (b) appends one row to the events table via `session.add`.
The helper does **not** commit; the caller's surrounding transaction (e.g., the API's explicit `BEGIN IMMEDIATE` block, or the worker loop's claim transaction) commits both rows together so the projection and the log share a transactional boundary.

Event `kind` is a closed enum: `queued`, `claimed`, `phase`, `cancelled`, `failed`, `succeeded`, `upload_pending`, `recovered_stale`, `source_enriched`.
The `payload_json` column carries a discriminated-union pydantic model `JobEventPayload`, with one variant per kind — for example `RecoveredStalePayload(stale_after_minutes: int, last_started_at: datetime)`, `PhasePayload(phase: Literal["preparing_input", "converting", "uploading"], reported_at: datetime)`, `SourceEnrichedPayload(columns_written: list[str], aizk_uuid: UUID)`.
Unknown payload shapes are rejected at write time by pydantic validation.

The subprocess already produces structured `phase` / `completed` / `failed` / `cancelled` events on `mp.Queue`.
The parent's supervision loop drains these and currently logs them.
Under this change, only `phase` events produce a log row (kind `phase`, scoped to `(job_id, attempt)`).
Subprocess terminal events (`completed` / `failed` / `cancelled`) continue to drive the parent's control flow but are NOT persisted directly — the orchestrator's `handle_job_error`, the uploader's success path, and the API's cancel handler each move from direct `job.status = ...` writes to `record_transition` calls, and their transition events are the canonical terminal record.
This keeps a single sanitized writer per terminal outcome (important because `handle_job_error` strips destination data out of egress-policy error messages before persistence).

`_write_source_enrichment` gets a `record_transition`-like sibling, `record_source_event`, that emits a `source_enriched` event whether the Source UPDATE succeeded or failed.
This stays best-effort — Source enrichment is advisory — but the audit record is no longer best-effort.

The `recover_stale_running_jobs` sweep is just another transition site: RUNNING → FAILED_RETRYABLE with a `recovered_stale` kind and a payload carrying `stale_after_minutes` and the original `started_at`.
This is what makes the change pay back the most for operators trying to understand "why did this attempt get retried."

## Schema Impact

**Database (SQLite):**

- New table `conversion_job_events` with columns: `id` (PK), `job_id` (FK → `conversion_jobs.id` with `ON DELETE SET NULL`), `aizk_uuid` (denormalized from `conversion_jobs.aizk_uuid`, indexed), `attempt` (int), `occurred_at` (timestamp), `kind` (enum string), `from_status` (nullable enum string), `to_status` (nullable enum string), `payload_json` (text).
- Indexes: `(job_id, occurred_at)` for per-job history queries; `(aizk_uuid, occurred_at)` for audit by Source identity (also survives job deletion); `(kind, occurred_at)` for fleet-wide audit queries.
- New Alembic migration creating the table.
- `aizk_uuid` is denormalized so that audit queries continue to work after a job is deleted via `_apply_job_delete` (the FK goes to NULL but the Source identity remains on the row).
- `ON DELETE SET NULL` is chosen over `CASCADE` to preserve the audit trail across operator deletions, and over `RESTRICT` to avoid breaking the existing delete endpoint contract.

**OpenAPI:** No changes.
No endpoints are added, modified, or removed.
The pre-change snapshot in `schemas/before/conversion-api-openapi.json` is the expected post-change snapshot.

## Decisions Carried Into Design

- `record_transition` is a **passive log**, not a state-machine validator.
  It writes whatever `(from_status, to_status)` pair the caller supplies.
  The transition matrix lives in `design.md` as documentation, not as a runtime enforcement point.
  Revisit if drift appears.
- `phase` events are **not deduplicated**.
  Every subprocess-reported phase produces one event row, scoped to `(job_id, attempt)`.
  Dedup is a query-time concern.
