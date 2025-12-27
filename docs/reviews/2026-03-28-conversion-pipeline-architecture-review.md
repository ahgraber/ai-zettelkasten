# Enterprise Architecture Review: Ingestion & Conversion Pipeline

**Date:** 2026-03-28
**Reviewer:** Claude (Enterprise Application Architect perspective)
**Scope:** `aizk.conversion` package — document ingestion and conversion pipeline
**Version:** 0.0.1

## Context

Reviewed: the `aizk.conversion` package — a document ingestion and conversion pipeline that accepts bookmarks from KaraKeep, converts content (HTML/PDF) to Markdown via Docling, and persists artifacts to S3.
The system consists of a FastAPI REST API, a polling-based background worker with subprocess isolation, SQLite persistence with Litestream replication, and S3-compatible object storage.

Version: 0.0.1. ~8,400 LOC across 72 Python files.
The worker module alone is 988 lines.

---

## The Good

**1.**
**Explicit error retryability is a genuinely strong pattern.**
Every exception class declares `retryable: ClassVar[bool]` at the class level.
This forces developers to make a conscious decision about failure semantics at definition time rather than at catch sites.
The worker's `handle_job_error()` reads this attribute to decide retry-vs-permanent without string matching or guesswork.
This is better than what most enterprise systems do.
It's principled, composable, and reviewable.

**2.**
**Subprocess isolation for conversion is the right call.**
Running Docling conversion in a separate process group (`os.setpgrp()`) means a segfault, OOM, or runaway conversion doesn't take the worker down.
The SIGTERM → wait → SIGKILL escalation with process group termination is correct.
The `spawn` context (not `fork`) avoids the classic Python fork-safety pitfalls.
This is production-grade process management.

**3.**
**Idempotency is first-class, not bolted on.**
The SHA-256 idempotency key derived from (UUID, version, docling config, picture description flag) combined with xxHash64 content deduplication before S3 upload means the system is safe to replay.
This is exactly the kind of thing that's painful to add later and cheap to have from the start.

**4.**
**The state machine is well-defined.**
`NEW → QUEUED → RUNNING → UPLOAD_PENDING → SUCCEEDED` with explicit `FAILED_RETRYABLE`, `FAILED_PERM`, and `CANCELLED` terminal states.
The transitions are guarded.
The stale job recovery (`RUNNING` > 30 min → `FAILED_RETRYABLE`) handles the worker-crash scenario.
The separation of `UPLOAD_PENDING` from `RUNNING` acknowledges that upload is a distinct failure domain.

**5.**
**The ADR practice is excellent.**
Nine ADRs with clear rationale, alternatives considered, and trade-offs documented.
The decisions are opinionated and well-reasoned (e.g., rejecting litellm as "dumpster fire", choosing SQLite over Postgres for self-hosted simplicity).
This is rare and valuable.

**6.**
**Spec-driven development with archived change history.**
The `.specs/` structure with baseline specs, delta changes, and archived proposals gives you a traceable design record.
The archived changes show the system's evolution over time.
This is better documentation practice than most enterprise teams manage.

---

## The Bad

**1.**
**The worker is a 988-line god module.**

`worker.py` contains: job polling, subprocess spawning, supervision, cancellation detection, timeout enforcement, status message collection, bookmark preparation, input preparation, conversion orchestration, artifact upload with retry, S3 deduplication, manifest generation, error handling, stale job recovery, and the main event loop.
All in one file.

This violates every modularity principle.
Any change to upload logic requires understanding the supervision loop.
Any change to error handling requires scrolling past conversion orchestration.
The cognitive load is high and the blast radius of changes is wide.
For a "critical application," this is a maintenance time bomb.

**2. `ConversionConfig()` is instantiated repeatedly throughout the codebase — no dependency injection.**

`worker.py:724`, `worker.py:861`, `worker.py:928`, `worker.py:976` — the config is constructed fresh from environment variables on almost every function call.
Same in `db.py:33`, `api/main.py:20`.
There's no central config instance, no injection, and no lifecycle management.
This means:

- Config is parsed from `.env` on every job poll (every 2 seconds)
- If the `.env` file changes mid-flight, different parts of the system could see different config
- Testing requires environment variable manipulation rather than passing a config object
- There's no validation that config is consistent at startup

**3.**
**No schema migration system.**

`create_db_and_tables()` calls `SQLModel.metadata.create_all()` — which only creates tables that don't exist.
It cannot alter existing tables.
Any column addition, type change, or index modification requires manual SQL or rebuilding the database from scratch.
For a v0.0.1 this is understandable, but you've declared Semantic Versioning and migration plans as requirements in CLAUDE.md.
There's no Alembic, no versioned migrations, nothing.
The moment you need to add a column to `conversion_jobs`, you have a problem.

**4.**
**The queue is a polling loop against SQLite, not an actual queue.**

The worker polls the database every 2 seconds with `BEGIN IMMEDIATE` + `SELECT ... WHERE status IN (QUEUED, FAILED_RETRYABLE) ORDER BY queued_at LIMIT 1`.
This works for single-worker deployments but has fundamental scaling limitations:

- `BEGIN IMMEDIATE` acquires an exclusive write lock on the _entire database_ for the duration of the transaction, blocking all other writes including the API
- Polling introduces latency (up to 2s per job) that compounds under load
- `queue_max_depth` is configured (1000) but **never enforced** — the submit endpoint doesn't check it
- `worker_concurrency` is configured (4) but the worker processes **one job at a time sequentially** in `run_worker()`.
  There's no thread pool, no async dispatch, no concurrent processing.

The gap between what the config implies (4 concurrent workers) and what the code does (serial polling) is a spec violation.

**5.**
**No health check endpoints.**

No `/health`, `/readiness`, or `/liveness` endpoints.
The API starts, calls `create_db_and_tables()`, and begins serving.
There's no validation that S3 is reachable, that credentials work, or that the database is in a consistent state.
An orchestrator (Docker, K8s, systemd) has no way to know if the service is healthy.
The worker has no health signal at all — it's an infinite loop with no external observability.

**6. `api_reload: bool = Field(default=True)` in production config.**

`config.py:102` — hot-reload is enabled by default.
This is a development convenience that should never be the default in a production config class.
In production, it causes the uvicorn reloader to watch the filesystem, restart on any file change, and potentially cause request drops during reload cycles.

**7.**
**Silent feature degradation with no operator feedback.**

If `CHAT_COMPLETIONS_BASE_URL` or `CHAT_COMPLETIONS_API_KEY` are unset, picture descriptions are silently disabled.
If `MLFLOW_TRACING_ENABLED` is false, all tracing is silently off.
If `LITESTREAM_S3_BUCKET_NAME` is empty, replication silently doesn't happen.
The operator gets no warning, no startup log, no health check failure — nothing.
They discover the gap when they look for data that doesn't exist.

---

## The Ugly

**1.**
**Single-threaded worker with no concurrency is architecturally broken for stated goals.**

`run_worker()` (line 973-988) is a `while True` loop that calls `poll_and_process_jobs()` synchronously.
`process_job_supervised()` blocks until the subprocess completes and upload finishes — which can take up to **2 hours** per job (`worker_job_timeout_seconds=7200`).
During that time, no other job is picked up.
No stale job recovery runs.
Nothing.

If you have 100 queued jobs and the first one takes 30 minutes, the other 99 wait.
The `worker_concurrency=4` config parameter is fiction — it's set but never read by any code.
There is no concurrent processing.

For a "critical application" that needs to process a backlog of content from KaraKeep, this is the single biggest architectural gap.
A 250-page PDF conversion with OCR and VLM picture descriptions could easily take 20+ minutes, during which the entire pipeline is frozen.

**2.**
**The database-as-queue pattern will not survive its first real load test.**

SQLite WAL mode allows concurrent readers, but `BEGIN IMMEDIATE` serializes all write transactions.
The poll loop acquires an exclusive write lock every 2 seconds.
The API also writes to the database (job submission, status updates, cancellation).
Under concurrent load:

- API write + worker poll = lock contention → `busy_timeout=5000ms` → 5-second stalls
- Multiple worker instances (if deployed) = mutual exclusion on every poll cycle
- Stale job recovery reads and writes in a separate transaction — more lock contention

SQLite is a reasonable _storage engine_ for a single-node system.
Using it as a _job queue_ with polling and exclusive locking is a fundamentally different access pattern that SQLite was not designed for.
The `busy_timeout` masks the problem until it doesn't.

**3.**
**Litestream replication + SQLite WAL + concurrent writers is a fragile combination.**

Litestream works by monitoring SQLite's WAL file and replicating it to S3.
It assumes a single writer process.
The system runs two processes (API + worker) that both write to the same SQLite database.
Litestream's documentation explicitly warns about multi-writer scenarios.
The combination of:

- API writes (job submission, cancellation)
- Worker writes (status transitions, error recording)
- Litestream checkpointing

creates a three-way race on the WAL file.
This may work in practice under light load, but under sustained write pressure, you risk replication gaps, checkpoint failures, or corruption that won't be detected until you try to restore from backup.

**4.**
**No graceful shutdown — data loss on SIGTERM.**

The worker's `run_worker()` is a bare `while True` loop with no signal handling.
When the process receives SIGTERM (container restart, deployment, systemd stop):

- The loop is interrupted wherever it happens to be
- If mid-upload: partial S3 artifacts with no cleanup
- If mid-conversion: subprocess is orphaned (it's `daemon=True`, so Python kills it, but the process group may not be cleaned up)
- If mid-database-write: SQLite handles this via WAL, but the job is left in `RUNNING` state until stale recovery kicks in (30 minutes later)

The API process fares slightly better because uvicorn handles SIGTERM with a drain period, but there's no application-level shutdown hook to stop accepting new work or flush state.

**5.**
**Error traceback information is lost.**

In `_process_job_subprocess()` (line 557-565), unexpected exceptions are caught with `except Exception as exc`, reported to the parent as `str(exc)` (just the message), and then `raise`d — into a subprocess that's about to die.
The traceback is gone.
In `handle_job_error()` (line 866), only `str(error)` is stored in the database.
No stack trace.
No structured exception context.
No logging of `exc_info=True`.

When a production conversion fails with `KeyError: 'content'`, you'll have the error message and error code in the database.
You won't have the stack trace, the locals, the call chain, or any context about what content was being processed.
For a critical application, this makes debugging from logs nearly impossible.

**6.**
**The engine cache is a process-global mutable dict with no thread safety.**

`db.py:27` — `_ENGINE_CACHE: dict[str, Engine] = {}` is a module-level dict.
`get_engine()` does a check-then-set without any locking.
In the API process (which runs under uvicorn with thread pool workers), concurrent requests could race on cache population.
SQLAlchemy `Engine` objects are thread-safe, but the cache population itself is not.
In practice, the worst case is creating two engines for the same URL (a resource leak, not a crash), but for a "critical application" this is sloppy.

**7.**
**The Containerfile runs as root.**

No `USER` directive.
The process runs as PID 1 as root inside the container.
Git is installed in the final image for reasons that aren't clear for the conversion runtime.
No health check instruction.

---

## Recommendations

### P0: Fix Before Any Production Traffic

| #   | Issue                         | Recommendation                                                                                                                                                                                                                                                            | Effort  |
| --- | ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------- |
| 1   | **Single-threaded worker**    | Implement a process pool or asyncio task pool in `run_worker()` that actually honors `worker_concurrency`. Use `concurrent.futures.ProcessPoolExecutor` or spawn N supervisor coroutines. The polling loop should dispatch jobs to a bounded pool, not block on each one. | Medium  |
| 2   | **No graceful shutdown**      | Register `signal.signal(SIGTERM, ...)` and `signal.signal(SIGINT, ...)` in both API lifespan and worker loop. On signal: stop accepting new jobs, wait for in-flight work (with timeout), flush database state.                                                           | Small   |
| 3   | **No health endpoints**       | Add `GET /health/live` (returns 200 if process running) and `GET /health/ready` (validates DB connectivity, S3 reachability, worker loop alive). Wire into container health check.                                                                                        | Small   |
| 4   | **Reload enabled by default** | Change `api_reload` default to `False`. Development overrides should be explicit, not production defaults.                                                                                                                                                                | Trivial |

### P1: Fix Before Relying on This in Any Workflow

| #   | Issue                            | Recommendation                                                                                                                                                                                                                                 | Effort |
| --- | -------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------ |
| 5   | **No migration system**          | Add Alembic with auto-generation. Create an initial migration from current schema. Every schema change gets a versioned migration script.                                                                                                      | Medium |
| 6   | **Error tracebacks lost**        | In the subprocess error handler, serialize `traceback.format_exception()` into the status queue message. In `handle_job_error()`, log with `logger.error(..., exc_info=True)`. Store structured error context, not just `str(error)`.          | Small  |
| 7   | **Silent config degradation**    | At startup (in `lifespan` and `run_worker`), log a summary of enabled/disabled features. Warn explicitly when picture descriptions, MLflow, or Litestream are disabled due to missing config. Validate S3 credentials with a HEAD bucket call. | Small  |
| 8   | **Config instantiation scatter** | Create the `ConversionConfig` once at process startup. Pass it through function arguments or use a simple module-level singleton with explicit initialization. Stop re-parsing `.env` on every function call.                                  | Medium |

### P2: Architectural Debt to Address

| #   | Issue                            | Recommendation                                                                                                                                                                                                                                                                                                                                                                        | Effort |
| --- | -------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------ |
| 9   | **988-line worker god module**   | Extract into: `worker/supervisor.py` (subprocess lifecycle), `worker/uploader.py` (S3 upload with retry), `worker/poller.py` (job selection and dispatch), `worker/recovery.py` (stale job recovery), `worker/loop.py` (main event loop). Each module gets its own tests.                                                                                                             | Large  |
| 10  | **Database-as-queue**            | For the immediate term: enforce `queue_max_depth` in the submit endpoint. Add an index hint on `(status, earliest_next_attempt_at, queued_at)` as a composite index to avoid full scans. Longer term: evaluate whether the polling pattern should be replaced with `LISTEN/NOTIFY` (if migrating to Postgres) or a proper queue (Redis, SQS) as the ADR-008 Prefect decision implies. | Medium |
| 11  | **Litestream multi-writer risk** | Document the write topology explicitly. If both API and worker write to the same SQLite file, validate that Litestream handles this correctly under your load profile. Consider designating one process as the primary writer or routing all writes through the API.                                                                                                                  | Medium |
| 12  | **Container hardening**          | Add `USER nonroot`, remove git from final image (multi-stage build), add `HEALTHCHECK` instruction, pin base image digest.                                                                                                                                                                                                                                                            | Small  |

### P3: Invest When Scaling

| #   | Issue                   | Recommendation                                                                                                                                                                                                                                                                                                                                                                                           | Effort |
| --- | ----------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------ |
| 13  | **Distributed tracing** | Add OpenTelemetry spans that correlate API request → job creation → worker pickup → subprocess conversion → upload → completion. This replaces the ad-hoc phase logging with structured, queryable traces. MLflow tracing for LLM calls can coexist.                                                                                                                                                     | Large  |
| 14  | **Prefect integration** | ADR-008 chose Prefect for orchestration, but the current implementation is a hand-rolled polling loop. Either integrate Prefect (which gives you concurrency, retries, observability, and scheduling for free) or explicitly revise the ADR to document why you chose not to. The current state is neither — you have the complexity of a custom worker with none of the benefits Prefect would provide. | Large  |
| 15  | **Webhook integration** | ADR-009 designed the KaraKeep webhook push model. Implementing it eliminates polling latency on the ingestion side and makes the system event-driven end-to-end.                                                                                                                                                                                                                                         | Medium |

---

## Summary Verdict

The design _thinking_ is strong — the ADRs, specs, idempotency model, error classification, and subprocess isolation show clear architectural intent.
The _execution_ has not caught up to the ambition.
The single-threaded worker, database-as-queue, absent health checks, no graceful shutdown, and lost error context would each individually be concerning for a critical application.
Together, they mean the system is not production-ready.

The good news: none of these are foundational design flaws.
They're implementation gaps.
The architecture can support concurrent processing, proper health signaling, and graceful lifecycle management without a redesign.
The P0 items (concurrent worker, graceful shutdown, health checks) are the minimum bar before any real traffic.
The P1 items (migrations, error context, config validation) are the minimum bar before you can debug problems without SSH-ing into the box.

---

## Appendix: Spec Impact Analysis

Each recommendation was evaluated against the existing baseline specs (`conversion-api`, `conversion-worker`, `worker-process-management`, `mlflow-llm-tracing`, `conversion-ui`) to determine whether it requires spec changes or is a pure implementation fix.

### Pure Implementation — No Spec Changes Needed

| #      | Finding                      | Why                                                                                                                                                                                                                                                |
| ------ | ---------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| P0 #1  | Single-threaded worker       | **The spec already requires this.** `conversion-worker/spec.md`: "The system SHALL process conversion jobs with a configurable number of parallel workers (default: 4)." The implementation is non-conformant — this is a bug, not a missing spec. |
| P0 #4  | Reload default=True          | Config default. No spec governs this.                                                                                                                                                                                                              |
| P1 #5  | No migration system          | Tooling/process concern. CLAUDE.md requires migration plans after first MINOR release, but no spec covers migration tooling.                                                                                                                       |
| P1 #6  | Error tracebacks lost        | The worker spec already requires "structured logs with trace context." Capturing stack traces is an implementation improvement within that existing requirement.                                                                                   |
| P1 #8  | Config instantiation scatter | Internal architecture. No spec dictates DI patterns.                                                                                                                                                                                               |
| P2 #9  | 988-line god module          | Internal refactoring. Specs don't prescribe module structure.                                                                                                                                                                                      |
| P2 #11 | Litestream multi-writer risk | Operational/infrastructure concern. No spec covers write topology.                                                                                                                                                                                 |
| P2 #12 | Container hardening          | Ops/infra. No spec covers container configuration.                                                                                                                                                                                                 |

**8 of 15 items are implementation-only.**
Notably, the biggest P0 item (worker concurrency) is already spec'd — the code just doesn't conform.

### Require Spec Changes

| #      | Finding                                        | Which Spec            | What's Needed                                                                                                                                                                                                                         |
| ------ | ---------------------------------------------- | --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| P0 #3  | Health endpoints                               | **conversion-api**    | New requirement: liveness and readiness endpoints with defined checks (DB reachable, S3 credentials valid). The current spec has no health surface at all.                                                                            |
| P1 #7  | Silent config degradation / startup validation | **conversion-worker** | New requirement: on startup, log a summary of enabled/disabled optional features and validate reachability of required external services (S3, KaraKeep). The current spec says "load configuration" but not "validate configuration." |
| P2 #10 | Queue depth enforcement                        | **conversion-api**    | New requirement: reject job submissions (503) when queue depth exceeds `queue_max_depth`. The config field exists, the worker spec mentions concurrency limits, but neither spec says the API should enforce backpressure.            |

**3 items need spec amendments** — all are additive (new requirements), not changes to existing ones.

### Borderline — Arguable Either Way

| #      | Finding             | Assessment                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| ------ | ------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| P0 #2  | Graceful shutdown   | The worker-process-management spec guarantees workspace cleanup "regardless of outcome" and defines subprocess termination sequences — but it is **silent on the worker loop's own SIGTERM handling**. You could implement signal handling as fulfilling the existing cleanup guarantee, or add a small explicit requirement to worker-process-management: "The worker process SHALL handle SIGTERM by completing in-flight jobs (with timeout) before exiting." Recommendation: add it — the current spec covers job-level lifecycle but not process-level lifecycle. |
| P3 #13 | Distributed tracing | The worker spec already requires "operational metrics" and "structured logs with trace context." You could argue OTel spans are a better implementation of those requirements. But correlated trace IDs across API → worker → subprocess is a new cross-cutting requirement that goes beyond what "structured logs" means today.                                                                                                                                                                                                                                       |
| P3 #14 | Prefect integration | ADR-008 chose Prefect, but the worker spec describes a self-contained polling model. Integrating Prefect would require **significant rewrites** to the worker spec — the polling loop, job selection, retry scheduling, and stale recovery would all change. This is more "new spec" than "spec amendment."                                                                                                                                                                                                                                                            |
| P3 #15 | Webhook integration | ADR-009 exists but has no spec yet. **New spec needed**, not a change to existing ones.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |

### Summary

- **8 items**: implementation only, no spec changes
- **3 items**: clear spec amendments needed (health endpoints, startup validation, queue backpressure)
- **4 items**: borderline — 1 small addition (graceful shutdown), 3 that are new specs or major rewrites (tracing, Prefect, webhooks)

The P0 work is almost entirely implementation.
The only P0 spec change is adding health endpoints to the API spec.
