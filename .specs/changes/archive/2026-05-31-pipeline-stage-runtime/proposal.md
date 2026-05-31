# Proposal: Pipeline-Stage Runtime

> This change is the **implementation prerequisite** for the contextualization and mention-extraction workers (build order: this change → chunk-persistence-contextualization → mention-extraction-foundation), even though it was specced after them.
> It is a **behavior-preserving structural refactor** of the conversion stage's run machinery into a shared runtime; the existing conversion test suite is the regression net.

## Intent

The conversion stage built a substantial, reliability-critical machine for running queued work: a worker runner, an append-only job/transition event log, a pluggable adapter/composition pattern, and an HTMX operator UI.
The graph stages now coming — chunk **contextualization** (LLM per chunk), **mention extraction** (NER per chunk), and later **entity canonicalization** (re-clustering jobs) — each need the same machine.
Re-implementing it per stage triples the surface for the very bugs the conversion archive already fixed (graceful shutdown, concurrency, queue backpressure, stale-job recovery, startup validation, health endpoints).

This change extracts that machine into a small **primitives package** (`aizk.pipeline`), not a framework: a claim/drain/cancel/timeout runner over a stage-supplied **handler protocol**, a separable run/dataset-version primitive, and a transition-event helper.
Each stage keeps its own tables and unit identities; conversion is re-pointed to consume the primitives as the first adapter.
It is a **behavior-preserving extraction of conversion's machinery plus additive runtime primitives** for the future stages — the conversion behavior is preserved (its test suite is the regression net), while the run primitive, the handler-protocol seam, and shared event/run tables are genuinely new (additive) surface that future stages will use.

The code already leans this way: the conversion job event log **denormalized `aizk_uuid` specifically so "future processing-stage event tables that share the same Source identity can be queried alongside"**, and `pluggable-pipeline` already separates adapters from the orchestrator via protocols + a wiring composition root.
This change finishes that generalization rather than inventing it.

## Scope

Scope is the **core primitives** (decided): the runner over a handler protocol, the run/dataset-version primitive, the transition-event helper, the process-management lifecycle, and observability/startup.
The operator-UI scaffold and the adapter-composition / resolver-chain generalization are both **deferred** — see Out of scope.

**In scope (new `pipeline-stage-runtime` capability):**

- **Runner over a stage-supplied handler protocol.**
  Claim/lease loop, bounded concurrency, eligible-in-submission-order processing, queue backpressure, graceful drain on termination signals (bounded drain timeout; none left running), cancellation within a bounded interval, wall-clock timeout, graceful-before-forceful termination, no orphan descendants, optional per-stage subprocess isolation, stale-unit recovery recording its cause, and cleanup after any outcome — all driven through a protocol each stage implements over its **own** tables.
  No universal work-unit table.
- **Generic work-unit lifecycle + state machine.**
  A generic lifecycle (`queued → running → {succeeded, failed, cancelled, timed-out}`) with retryable/permanent classification of failures; stage statuses map onto it so the runner reasons about progress and retry uniformly.
- **Stage-run / dataset-version primitive (CONFIRMED), independent of execution.**
  A `run` record keyed by `(stage, scope_key)` carrying version stamps, `input_fingerprint`, `supersedes_run_id`, and `status` (active|superseded); immutable run-produced rows; **atomic run-level invalidation** (recording a new run and superseding the prior happen in one transaction; ≤1 active per `(stage, scope_key)`, never a gap).
  The stage defines its own `scope_key` (per-document, per-chunk, corpus-wide).
  Compaction of superseded runs is the separate `artifact-compaction-retention` change.
  Determinism asymmetry the consumers established: content-deterministic stages (chunking) use content-addressed, run-independent row ids with append-only run membership; model/input-dependent stages (contextualization, extraction) use run-scoped row ids.
- **Transition-event helper + cross-stage source identity.**
  An append-only transition event written in the **same transaction** as the authoritative status change (the `record_transition` pattern), with typed per-kind payloads; every work-unit and event carries the `aizk_uuid` source identity so a source's progress is resolvable across stages.
  Whether events live in a shared table or per-stage tables with a union view is a `design.md` decision.
- **Cross-cutting runtime services:** startup validation gating work acceptance, structured logging with trace context, metrics, and `setproctitle` process-role identification — runtime concerns, not per-stage reimplementations.

**Spec approach: additive now, reconcile at the end (decided).**

- This change writes one **new `pipeline-stage-runtime` capability spec** carrying the generic contracts.
  During the extraction, `conversion-worker`, `worker-process-management`, and `conversion-ui` are left intact (`design.md` notes they are now realized via the runtime), and conversion is re-pointed to consume it with its existing test suite as the regression net.
- A **final reconcile task** then relocates the now-duplicated generic contracts out of `worker-process-management` and `conversion-worker` into `pipeline-stage-runtime` (MODIFIED/REMOVED deltas with `> Previously:` provenance), leaving the conversion specs holding only conversion-specific behavior.
  Doing this last — after the extraction is proven green — keeps the risky structural move and the spec reconciliation separate.

**Out of scope:**

- **Generalizing the adapter-composition / resolver-chain** (`pluggable-pipeline`'s fetcher/converter registries, resolver-closure validation, capability descriptors, source-ref unions, egress policy).
  It is the most conversion-shaped piece, and contextualization/extraction adapters register on the core primitives without it.
  Deferred to a follow-up refactor; `pluggable-pipeline` stays conversion-owned for now.
- **Generalizing the operator UI.**
  The UI scaffold is the least-proven seam — only conversion has one.
  Conversion keeps its UI as-is; contextualization and extraction build their own operator views in their own changes; a shared UI is extracted later, once a second consumer proves the common shape (Rule of Three).
  This change ships no generic UI.
- Any behavior change, new endpoint, new stage, or new feature — those belong to the consuming changes, not this refactor.
- Building the contextualization / extraction / canonicalization adapters themselves (their own changes).
- Multi-writer / Postgres migration — unchanged from ADR-003.

## Approach

> Mechanism sandbox; formalize into `design.md` when this draft is taken up.

- **Target package:** a new `aizk.pipeline` top-level package holds the primitives; `aizk.core` stays low-level shared (e.g. `database.py`).
  Conversion-specific code (docling subprocess, egress/SSRF policy, S3 upload, `ConversionJob` and its semantics) stays in `aizk.conversion` as the first adapter.
- **Functional-core / imperative-shell:** keep the unit-of-work logic (adapter) separable from the I/O shell (claim/commit/transition), so stages are testable without the runner.
- **Composition over inheritance:** stages compose the primitives by supplying an adapter + repository implementation, not by subclassing a base worker — avoid a deep worker hierarchy.
- **Strangler sequencing:** introduce the primitives, port conversion onto them behind the handler protocol, delete the conversion-local duplicates, all under the existing conversion tests.
  Structural commit(s) separate from any later behavior work (modularity-skill hard rule: never mix behavior and structure).

### Source material to extract from (for a cold pickup)

- Worker runtime: `src/aizk/conversion/workers/{loop,orchestrator,shutdown,supervision,types,errors}.py` (keep `converter.py`, `fetcher.py`, `uploader.py` as conversion adapters).
- Job + event log: `src/aizk/conversion/datamodel/{job,events,source}.py` (note `events.record_transition` and the `aizk_uuid` denormalization rationale in `.specs/changes/archive/2026-05-18-conversion-job-event-log/design.md`).
- Adapter/composition: `src/aizk/conversion/wiring/` + the `pluggable-pipeline` capability spec.
- Operator UI: `src/aizk/conversion/api/routes/{jobs,health,ui}.py` + `templates/jobs.html`, `templates/jobs_panel.html`.
- Reliability behaviors to preserve verbatim: the archived changes `worker-concurrency`, `worker-graceful-shutdown`, `queue-backpressure`, `startup-validation`, `health-endpoints`, `worker-process-management`.
- DB/runtime: `src/aizk/core/database.py`; ADR-003 (SQLite + WAL + single serialized writer + Litestream).

## ADR

The architectural decision is recorded as an addendum to `docs/decision-record/009-orchestration.md` (orchestration is the runtime's nearest neighbor).
The ADR keeps the current decision unchanged — no external orchestrator now — and narrows the migration trigger to named missing primitives rather than general workflow complexity.

## Resolved Questions

Decided: **scope** is the core primitives (UI and resolver-chain generalization deferred, above); **spec approach** is additive + a final reconcile task (above); **package home** is `aizk.pipeline` (above); **ADR home** is an ADR-009 addendum.
Settled in `design.md`:

- **Repository protocol responsibilities.**
  The runner is the current embedded engine implementation, not a universal engine-neutral seam.
  Engine-owned responsibilities are work discovery, claim/lease, eligibility ordering, retry scheduling, timeout/cancel/drain, and stale recovery.
  Stage-owned responsibilities are dependency validation, unit-of-work execution, result→terminal-outcome mapping, retryability classification, transient cleanup, declared timeout/concurrency needs, status/event projection writes, and run `scope_key`.
- **Stage-run primitive shape — determinism asymmetry.**
  Row-identity scoping is left to the adapter: deterministic artifacts may use content-addressed IDs + membership rows; model/config-dependent artifacts use generation-scoped IDs.
- **Transition events: shared table vs per-stage + union view.**
  Cross-stage progress uses one shared `pipeline_events` table keyed by source identity.
- **Migration-tree placement.**
  The runtime's run/event tables relative to the conversion migration tree and the graph tables `chunk-persistence-contextualization` added (which chose the shared conversion DB).
  The conversion Alembic tree owns all tables in one linear migration history because ADR-003 keeps one SQLite database and one metadata/parity surface.
