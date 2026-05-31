# `aizk.pipeline` — pipeline-stage runtime primitives

Stage-agnostic primitives a processing stage builds on to run as a supervised, durable, observable unit of work.

This package is deliberately a **set of primitives, not a framework**.
Each stage owns its own work-unit tables and identities and consumes these primitives through a narrow protocol.
`aizk.pipeline` stays import-independent of any stage (`aizk.conversion`, the graph stages, …) — consumers import the runtime, not the other way around.

## The division: engine vs. domain

The runtime separates a stage's **domain core** (what to do for one unit of work) from the **orchestration engine** (how units get discovered, run, retried, drained, and recovered).

- **The engine** is [`StageRunner`](runner.py) — the _current embedded engine implementation_, not a permanent framework.
  It owns: work discovery scheduling, the claim/lease loop, bounded concurrency with **claim/dispatch order** following submission order (eligible units are claimed oldest-first; this is _not_ a guarantee about worker-thread start timing — see below), retry-wait gating, signal handling + graceful drain, wall-clock timeout enforcement, graceful-before-forceful termination, stale-unit recovery, and lifecycle observability.
- **The domain** is a stage's [`StageHandler`](handler.py) implementation.
  It owns: startup dependency validation, the store-specific discovery/claim/transition queries, the unit-of-work (`execute`), mapping a result/exception to a generic outcome (`map_result`), transient-resource cleanup, the cooperative `cancel` hook, and its timeout/concurrency/isolation declarations.

This split is the durable seam: the domain core survives an engine swap; the runner is the part that would be replaced if an external orchestrator is ever adopted (see [ADR-009](../../../docs/decision-record/009-orchestration.md)).
The narrow surface — `execute`/`map_result`/`cleanup` carry no `Session` or claim semantics — is what keeps a stage testable today and portable later.

**Execution model:** thread-pool + optional subprocess isolation, _not_ asyncio.
In-process units run in a `ThreadPoolExecutor`; a stage may opt into subprocess isolation, for which the runner/adapter guarantee graceful-before-forceful termination with no orphaned descendants.

## Primitives

### Lifecycle & retry classification — [`lifecycle.py`](lifecycle.py)

A generic work-unit lifecycle every stage maps onto: `queued → running → {succeeded, failed, cancelled, timed_out}`.
A `failed` outcome is classified `retryable` or `permanent` (`RetryClass`).
`map_result` returns a `TerminalOutcome` (status + retry class); the runner reasons only over this generic vocabulary, never a stage's private statuses.
`is_terminal` / `TERMINAL_STATUSES` identify terminal states.

### Stage contract — [`handler.py`](handler.py)

`StageHandler[WorkUnitHandle]` is the protocol a stage implements over its own store (the handle is whatever opaque id the stage uses, e.g. a job id):

- `validate_dependencies()` — startup gate; raise if a required dependency is missing.
- `claim_next(session)` / `recover_stale(session)` — the stage's eligibility/claim and stale-recovery queries, run inside the runner-owned transaction.
- `execute(handle) -> StageResult` — the unit of work (no status writes; side effects the adapter owns).
  Raise on failure.
- `map_result(result | exception) -> TerminalOutcome` — classify success/failure into the generic lifecycle.
- `finalize(session, handle, outcome)` — write the terminal status transition, inside the runner transaction.
- `cleanup(handle)` / `cancel(handle)` — release transient resources; cooperatively cancel a running unit.
- `scope_key(handle)`, `timeout`, `concurrency_limit`, `isolation`, `stage` — the stage's configuration.

### Engine — [`runner.py`](runner.py)

`StageRunner(repository, engine, *, shutdown=, metrics=, drain_timeout=, poll_interval=, stale_recovery_interval=, cancel_grace=, clock=, force_exit=)`.

- `run()` — the full supervised loop: install signal handlers, validate dependencies, set the process title, claim/process within the concurrency bound until a shutdown signal, then drain within the bounded timeout.
  Returns an exit code (`0` clean, `1` forced).
- `run_until_idle()` — a deterministic driver for tests and one-shot batch runs.
- `cancel_handle(handle)` / `recover_stale()` — operational entry points.

`StageMetrics` is the operational-metrics sink (counters/gauges); `InMemoryMetrics` is the default in-process implementation.

**Concurrency contract (precise guarantee).**
The runner provides a **claim/dispatch-order** guarantee, not a worker-start-order one: it claims eligible units one slot at a time in the repository's submission order (oldest-first, no starvation), so the first `limit` units claimed are the first `limit` submitted.
Once a unit is dispatched to the pool, the worker threads may begin in any order — there is **no start-order handshake**, and none is needed under bounded concurrency. (Completion order is likewise unconstrained.) Tests assert the claimed _set_, not the thread start _sequence_.

### Run / dataset-version primitive — [`run.py`](run.py)

`PipelineRun` + `record_run` version a stage's model/config-dependent derived output.
At most one run per `(stage, scope_key)` is `active` (enforced by a partial unique index); recording a new run and superseding the prior one is atomic — never two active, never a gap.
Runs record an input fingerprint and version stamps for reproducibility, and are immutable except for the active→superseded lifecycle transition.
_No orchestrator provides this_ — it is domain state that survives any engine.

### Transition-event log — [`events.py`](events.py)

`record_transition` co-commits a work-unit's status change with an append-only `PipelineEvent` row **in the same transaction** — a committed status change never exists without its event, and vice versa.
The shared `pipeline_events` table is keyed by the cross-stage source identity (`aizk_uuid`) plus stage / work-unit ref / run ref, so a source's progress is resolvable across stages.
Treat it as the product-facing read-model/audit projection — query it, not the engine's internals.

### Shutdown — [`shutdown.py`](shutdown.py)

`ShutdownController` is per-runner-instance drain state; OS signals are delivered process-wide by a module-level dispatcher that **broadcasts** to every registered controller, so two runners can share one process and both observe a signal.
`force_exit` (`os._exit`) is the forced-drain path: it bypasses the non-daemon thread pool's atexit join that would otherwise hang on a stuck in-process unit.

#### Process-global ownership and broadcast semantics

A runner touches several process-global facilities.
Their ownership when **two or more runners share one process** is:

- **SIGTERM / SIGINT** — _process-global, dispatcher-owned._
  `signal.signal` installs exactly one handler per signal for the whole process, so no single controller can own the disposition.
  The module-level `_dispatcher` installs the OS handlers **once** (on the main thread; an off-main-thread register is recorded for broadcast but skips the OS install) and **broadcasts** every signal to all registered controllers: the first signal requests each controller's graceful drain, a second requests each controller's immediate termination.
  Every runner in the process therefore observes one signal.
  Drain _bookkeeping_ stays per-`ShutdownController`.
- **`os._exit` / `force_exit`** — _whole-process termination._
  The forced-drain path terminates the **entire process**, not one runner: one runner's forced exit tears down every other runner, thread, and pool in the process.
  It is a deliberate last-resort escape for a stuck/uncooperative in-process unit; cooperative shutdown drains per-runner via `ShutdownController` instead.
- **`atexit`** — _bypassed on the forced path by design._
  A clean drain lets the `ThreadPoolExecutor` join its non-daemon workers at interpreter exit.
  The forced path uses `os._exit` precisely to **skip** that `atexit`-time join, which would otherwise hang forever on a stuck in-process worker that ignores cooperative cancellation.
  So the forced path runs no `atexit` handlers; the clean path runs them normally.
- **`setproctitle`** — _last-write-wins on a single process-global slot._
  The process title is one global slot, so when two runners each `set_process_title()` the most-recently-set stage role is the one operators see.
  The title is an advisory role hint, not a per-runner identifier.
- **`ThreadPoolExecutor` lifecycle** — _per-runner-instance ownership._
  Each runner constructs and owns **its own** pool (sized to its repository's `concurrency_limit`) inside `run()` / `run_until_idle()` and shuts it down in the same scope.
  Pools are not shared across runners; two runners in one process run two independent pools.
- **Logging setup** — _process-global, configured once by the entrypoint._
  The runner only **emits** structured logs (it never calls `logging.basicConfig` or installs handlers); root-logger configuration is process-global and is the responsibility of the process entrypoint, configured once for all runners sharing the process.

## How a stage consumes the runtime

1. Implement `StageHandler` over the stage's own tables (the domain core).
2. Add the stage's tables to the shared Alembic tree; use `record_run` for versioned derived output and `record_transition` for status changes.
3. Construct and run the runner:

```python
repo = MyStageHandler(config, ...)
runner = StageRunner(repo, engine, shutdown=ShutdownController())
exit_code = runner.run()
```

Adding or changing a stage means writing/altering its `StageHandler`; it does not require modifying the runner.

## Transaction discipline

The runner owns the `Session` and opens the `BEGIN IMMEDIATE` transaction, passing it into `claim_next` / `recover_stale` / `finalize`; the repository runs its query + transition inside it and **never commits**.
This preserves the single-serialized-writer / co-commit invariant (status change and its event commit together or not at all) and keeps the SQLite + Litestream assumption (one writer) intact.

## Worked example

`aizk.conversion.handler.ConversionStageHandler` is the reference implementation: it carries the conversion unit-of-work (fetch → convert → upload → enrich) behind this protocol, maps conversion's statuses onto the generic lifecycle, and runs under `StageRunner` via `aizk.conversion.processing.worker`.
