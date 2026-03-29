# Design: Worker Concurrency

## Context

The worker currently processes one job at a time in a synchronous loop.
The config field `worker_concurrency` (default: 4) exists but is unused.

Docling loads multiple GPU-resident models (layout, OCR, table structure, formula) per subprocess.
Each spawned subprocess loads its own copy.
Multiple concurrent subprocesses on a single GPU will OOM — Docling has no cross-process model sharing, no VRAM limiting, and known memory leaks per DocumentConverter instance.

The worker's job pipeline has three phases with different resource profiles:

1. **Preflight** (CPU/network): fetch bookmark from KaraKeep, validate content
2. **Conversion** (GPU): spawn Docling subprocess, supervise until complete
3. **Upload** (network): upload artifacts to S3 with retry

Only phase 2 requires GPU.
Phases 1 and 3 can safely overlap across jobs.

## Decisions

### Decision: ThreadPoolExecutor with main-thread polling

**Chosen:** The main thread polls for work and dispatches jobs to a `ThreadPoolExecutor`.
Worker threads run `process_job_supervised()` independently.

**Rationale:** Only one thread executes `BEGIN IMMEDIATE` (the main thread), eliminating SQLite write lock contention during job claiming.
Worker threads only write short status updates.
`process_job_supervised()` is already stateless and thread-safe — it gets its own engine, spawns its own subprocess, and manages its own supervision loop.

**Alternatives considered:**

- N worker threads each polling independently: causes `BEGIN IMMEDIATE` contention — all N threads race for the same SQLite write lock on every poll cycle.
  Most attempts fail and retry, wasting busy_timeout budget.
- Async rewrite with asyncio: the worker is synchronous, subprocess management uses blocking `process.join()`, and the supervision loop is inherently polling-based.
  An async rewrite would be a separate initiative with no clear benefit for this use case.

### Decision: Module-level GPU semaphore in orchestrator

**Chosen:** A `threading.Semaphore` in the orchestrator module gates subprocess spawning.
Preflight and upload run outside the semaphore.
Default: 1 concurrent GPU subprocess.

**Rationale:** Keeps the GPU gating colocated with subprocess management (orchestrator) rather than in the loop.
Each worker thread acquires the semaphore before spawning and releases after supervision completes.
The semaphore is transparent to supervision and shutdown — they see no change.

**Alternatives considered:**

- Semaphore in the loop (limit how many jobs are submitted): prevents pipeline parallelism — a job waiting for GPU blocks its thread's entire capacity, including preflight/upload phases.
- Separate GPU worker pool: adds complexity (two pools, routing logic) for marginal benefit.
  A semaphore achieves the same gating with less code.

### Decision: Greedy slot filling

**Chosen:** After claiming a job, the main thread immediately tries to claim another (up to `worker_concurrency`) before sleeping.

**Rationale:** Minimizes latency when multiple jobs are queued.
Without this, the loop would claim one job per poll interval, taking N * poll_interval seconds to fill N slots.

## Architecture

```text
                    Main Thread
                    ┌─────────────────────────────┐
                    │  while not shutdown:         │
                    │    stale recovery (periodic) │
                    │    reap completed futures    │
                    │    if slots available:       │
                    │      claim_next_job()        │
                    │      submit to executor      │
                    │    else: sleep               │
                    └─────────────┬───────────────┘
                                  │ submit
                    ┌─────────────┴───────────────┐
                    │     ThreadPoolExecutor       │
                    │     (max_workers=4)          │
                    └──┬──────┬──────┬──────┬─────┘
                       │      │      │      │
                    Thread  Thread  Thread  Thread
                       │      │      │      │
                    ┌──┴──┐   │      │      │
                    │  1. │ preflight (concurrent, no gate)
                    │  2. │ sem.acquire() ←── GPU semaphore (default: 1)
                    │  3. │ spawn + supervise subprocess
                    │  4. │ sem.release()
                    │  5. │ upload (concurrent, no gate)
                    └─────┘
```

## Risks

- **SQLite write contention under load:** Worker threads do short DB writes (status updates) that could contend with the main thread's `BEGIN IMMEDIATE`.
  Mitigation: `busy_timeout=5000` provides 5 seconds of automatic retry.
  With concurrency=4, contention is low.
- **Drain timeout coordination:** Per-job supervision and the aggregate drain both use `worker_drain_timeout_seconds`.
  If the aggregate timer fires slightly before per-job timers, futures may not yet be complete.
  Mitigation: the aggregate wait uses `drain_timeout + 15s` buffer to outlast per-job drains.
- **Threads blocked on GPU semaphore during shutdown:** A thread waiting on `sem.acquire()` won't respond to shutdown until it acquires.
  Mitigation: during drain, the supervision loop inside the GPU-holding thread force-terminates its subprocess, releasing the semaphore for waiting threads.
  Those threads then enter their own supervision+drain and exit normally.
