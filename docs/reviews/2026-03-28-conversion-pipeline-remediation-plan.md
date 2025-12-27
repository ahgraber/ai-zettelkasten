# Conversion Pipeline Remediation Plan

**Date:** 2026-03-28
**Source:** [Architecture Review](2026-03-28-conversion-pipeline-architecture-review.md)
**Scope:** Sequenced mitigation of all 15 findings from the architecture review

## Principles

- **Spec changes are just-in-time.**
  Each spec amendment lands as a discrete work item immediately before its associated implementation, not batched up front.
- **Each work item is discrete.**
  Items within a phase can be completed, reviewed, and merged independently.
  Dependencies between items are noted where they exist.
- **Outcomes, not tasks.**
  Each item defines what changes and what that achieves, not how to implement it.

---

## Phase 1: Foundation

**Gate:** Subsequent phases are safer and less error-prone after these structural changes land.

No spec changes required.
All items are internal refactoring or tooling with no behavior change.

| Item                                | Review Finding                       | Outcome                                                                                                                                                                                                                                   | Status |
| ----------------------------------- | ------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------ |
| 1a. Production-safe config defaults | P0 #4 — Reload enabled by default    | `api_reload` defaults to `False`. Development conveniences require explicit opt-in, not production opt-out.                                                                                                                               | Done   |
| 1b. Config singleton                | P1 #8 — Config instantiation scatter | Configuration is parsed once at process startup and threaded through the system. Eliminates repeated `.env` parsing, enables consistent config in tests without env var manipulation.                                                     | Done   |
| 1c. Schema migration system         | P1 #5 — No migration system          | Alembic (or equivalent) is initialized with a baseline migration matching the current schema. All future schema changes go through versioned migration scripts. Unblocks any data model changes in later phases.                          | Done   |
| 1d. Worker module decomposition     | P2 #9 — 988-line god module          | `worker.py` is split into focused modules (supervision, upload, polling, recovery, loop). Each module has a single responsibility and its own test surface. Prerequisite: 1b (config singleton), so config can be passed through cleanly. | Done   |

**Phase 1 ordering:** 1a is independent. 1b and 1c are independent of each other. 1d depends on 1b.

---

## Phase 2: Core Reliability

**Gate:** The system can receive real traffic — jobs are processed concurrently, processes shut down cleanly, and orchestrators can probe health.

| Item                        | Review Finding | Spec Change                                                                                                                                         | Outcome                                                                                                                                                                              | Status |
| --------------------------- | -------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------ |
| 2a. Spec: graceful shutdown | P0 #2          | Amend **worker-process-management** spec: add requirement that the worker process handles SIGTERM/SIGINT by draining in-flight work before exiting. | Specifies process-level lifecycle (the current spec only covers job-level lifecycle).                                                                                                | Done   |
| 2b. Graceful shutdown       | P0 #2          | —                                                                                                                                                   | Worker and API handle termination signals. In-flight jobs complete (with timeout) before process exit. Jobs are not left in RUNNING state for 30 minutes after a routine deployment. | Done   |
| 2c. Worker concurrency      | P0 #1          | None — spec already requires it                                                                                                                     | The worker honors `worker_concurrency` and processes N jobs in parallel. A 30-minute PDF conversion no longer blocks the entire pipeline. Resolves the spec conformance gap.         | Done   |
| 2d. Spec: health endpoints  | P0 #3          | Amend **conversion-api** spec: add liveness and readiness endpoint requirements with defined health checks.                                         | Specifies the health surface the API must expose.                                                                                                                                    | Done   |
| 2e. Health endpoints        | P0 #3          | —                                                                                                                                                   | API exposes liveness and readiness probes. Readiness validates DB connectivity and S3 reachability. Orchestrators (Docker, K8s, systemd) can detect and restart unhealthy instances. | Done   |

**Phase 2 ordering:** 2a → 2b (spec before implementation). 2c is independent. 2d → 2e (spec before implementation).
No cross-dependencies between the three streams (shutdown, concurrency, health).

---

## Phase 3: Operational Maturity

**Gate:** The system is debuggable and diagnosable without SSH access or ad-hoc database queries.

| Item                         | Review Finding | Spec Change                                                                                                                                                     | Outcome                                                                                                                                                                                                          | Status |
| ---------------------------- | -------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------ |
| 3a. Spec: startup validation | P1 #7          | Amend **conversion-worker** spec: add requirement to validate external service reachability on startup and log a summary of enabled/disabled optional features. | Specifies fail-fast behavior and feature visibility at startup.                                                                                                                                                  | Done   |
| 3b. Startup validation       | P1 #7          | —                                                                                                                                                               | Misconfigured S3 credentials, missing KaraKeep endpoints, or disabled features (picture descriptions, MLflow, Litestream) are surfaced immediately at startup — not discovered hours later when data is missing. | Done   |
| 3c. Error tracebacks         | P1 #6          | None — covered by existing "structured logs with trace context" requirement                                                                                     | Stack traces from subprocess failures are captured and logged. Error context in the database includes structured information beyond `str(error)`. Operators can diagnose conversion failures from logs alone.    | Done   |
| 3d. Container hardening      | P2 #12         | None — ops/infra concern                                                                                                                                        | Container runs as non-root, final image excludes unnecessary packages, health check instruction is present, base image is pinned.                                                                                | Done   |

**Phase 3 ordering:** 3a → 3b (spec before implementation). 3c and 3d are independent of each other and of 3a/3b.

---

## Phase 4: Scaling Preparation

**Gate:** The system has explicit backpressure and documented safety properties for its persistence layer.

| Item                          | Review Finding | Spec Change                                                                                                             | Outcome                                                                                                                                                                                                                  | Status |
| ----------------------------- | -------------- | ----------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------ |
| 4a. Spec: queue backpressure  | P2 #10         | Amend **conversion-api** spec: add requirement to reject job submissions when queue depth exceeds the configured limit. | Specifies the API's backpressure behavior (currently the config field exists but is never enforced).                                                                                                                     | Done   |
| 4b. Queue backpressure        | P2 #10         | —                                                                                                                       | The API enforces `queue_max_depth`. Unbounded queue growth under sustained load is prevented. Clients receive a clear signal (503) to back off. Composite index on job selection query eliminates full table scans.      | Done   |
| 4c. Litestream write topology | P2 #11         | None — operational documentation                                                                                        | The multi-writer topology (API + worker both writing to SQLite) is explicitly documented with its constraints, verified failure modes, and guidance on when to designate a single primary writer or migrate to Postgres. | Done   |

**Phase 4 ordering:** 4a → 4b (spec before implementation). 4c is independent.

---

## Phase 5: Architecture Evolution

**Gate:** Not gated — these are investments made when scaling demands or strategic priorities justify them.

| Item                    | Review Finding | Spec Change                                      | Outcome                                                                                                                                                                                                                                               | Status |
| ----------------------- | -------------- | ------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------ |
| 5a. Distributed tracing | P3 #13         | New cross-cutting requirement or standalone spec | Correlated trace IDs across API → worker → subprocess → S3. Replaces ad-hoc phase logging with structured, queryable spans. Enables latency analysis and bottleneck identification.                                                                   |        |
| 5b. Prefect integration | P3 #14         | Major worker spec revision or new spec           | Orchestration responsibilities (concurrency, retries, scheduling, observability) move from the hand-rolled polling loop to Prefect, aligning with ADR-008. Alternatively, ADR-008 is revised to document the decision to stay with the custom worker. |        |
| 5c. Webhook integration | P3 #15         | New spec (ADR-009 design exists, no spec yet)    | KaraKeep push events trigger ingestion jobs directly, eliminating polling latency on the ingestion side. System becomes event-driven end-to-end.                                                                                                      |        |

**Phase 5 ordering:** Each item is independent. 5b has the largest blast radius and may influence whether 5a targets the custom worker or a Prefect-based architecture.

---

## Issue-to-Outcome Traceability

| Review Finding                               | Phase  | Outcome                                    |
| -------------------------------------------- | ------ | ------------------------------------------ |
| P0 #1 — Single-threaded worker               | 2c     | Spec conformance; parallel job processing  |
| P0 #2 — No graceful shutdown                 | 2a, 2b | Clean process lifecycle on deployments     |
| P0 #3 — No health endpoints                  | 2d, 2e | Orchestrator-visible health status         |
| P0 #4 — Reload enabled by default            | 1a     | Safe production defaults                   |
| P1 #5 — No migration system                  | 1c     | Safe schema evolution path                 |
| P1 #6 — Error tracebacks lost                | 3c     | Debuggable failure logs                    |
| P1 #7 — Silent config degradation            | 3a, 3b | Fail-fast on misconfiguration              |
| P1 #8 — Config instantiation scatter         | 1b     | Consistent, testable configuration         |
| P2 #9 — 988-line god module                  | 1d     | Maintainable, testable worker code         |
| P2 #10 — Database-as-queue / no backpressure | 4a, 4b | Bounded queue growth under load            |
| P2 #11 — Litestream multi-writer risk        | 4c     | Documented persistence safety properties   |
| P2 #12 — Container hardening                 | 3d     | Secure, observable container               |
| P3 #13 — Distributed tracing                 | 5a     | Correlated cross-process observability     |
| P3 #14 — Prefect integration                 | 5b     | ADR-aligned orchestration (or revised ADR) |
| P3 #15 — Webhook integration                 | 5c     | Event-driven ingestion                     |
