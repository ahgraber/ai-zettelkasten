# Orchestration for a Durable, Idempotent Content-Ingestion Pipeline — Reframed

## TL;DR

- **Reframe changes the recommendation.**
  For a durable, per-chunk-idempotent ingestion pipeline that lands in Neo4j, the prior agent-orchestration framing was misleading.
  With ingestion as the use case, **Restate (Virtual Objects keyed by content-hash) and Temporal (Workflow IDs as idempotency keys) are the two technically strongest control-flow options**, and **Dagster (asset-based, dynamic partitions per chunk) is the strongest data-pipeline alternative** and a serious contender — possibly a better fit than any of the original five if you can accept its operational footprint.
  Procrastinate, Absurd, and Ray drop in priority for different reasons.
- **The central question is per-stage idempotency, and only three of the five have a first-class primitive for it:** Temporal (Workflow ID dedup + activity-level idempotency keys derived from `workflow_run_id + activity_id`), Restate (`idempotency-key` HTTP header + per-key Virtual Object exclusivity), and Absurd (spawn-time `idempotency_key` parameter that returns the existing task on duplicate).
  Procrastinate gives you `queueing_lock` and `lock`, which are exclusivity primitives, not result-dedup primitives.
  Ray Data gives you batch-level lineage recovery, not per-item idempotency.
- **Recommendation hierarchy (ingestion-optimized):** (1) **Temporal** if you want a single workflow-per-URL with content-hash IDs, GPU/CPU task-queue routing, and a UI that already does failure forensics; (2) **Restate** if you want per-content-hash Virtual Object exclusivity and a single-binary footprint with the cleanest "only one ingestion per URL at a time" guarantee; (3) **Dagster** if you treat the knowledge graph as an asset graph and want chunk-level partitions, backfills, and lineage as first-class concepts; (4) **Absurd** for the simplest "one ingestion task per chunk with `idempotency_key=content_hash`" model, accepting current observability and partitioning gaps; (5) **Procrastinate** as a per-task queue with `queueing_lock` for per-URL exclusivity, with stage chaining done by hand; (6) **Ray** only as a model-serving / batch-embedding sidecar, not as the orchestrator.

---

## Key Findings

### What changes with the reframe

The earlier round leaned on agent-decomposition concerns: child-task `await`, multi-hop query orchestration, async MCP `search`/`edit`/`get_result` job IDs, and "is this technology good at orchestrating an LLM agent's plan."
None of that is the actual problem here.
The actual problem is:

1. Each ingested item (URL / PDF / image / document) is a small DAG: extract → chunk → fan-out per chunk → embed + NER + RE + linking + (optional LLM enrichment) → graph write.
2. Each stage must be idempotent, and the workflow must survive crashes mid-pipeline.
   The right orchestrator is the one whose **idempotency primitive matches the natural unit of re-runnable work** — for ingestion, that unit is a (content-hash, stage-name) pair.
3. Reprocessing is small-scoped: a single chunk gets re-extracted, only its downstream stages re-run, and nothing else cascades.
   This is a **firebreak** problem, not a blast-radius problem, and it favors orchestrators with addressable per-item state.
4. There is no human in the loop, no agent that decides what tool to call, no multi-step query plan to durably pause.
   The "durable agent loop" features (awakeables, signal-driven resumption, multi-tool decomposition) that drove the prior round's recommendations are largely overkill here.

This reframe **upgrades** Dagster (asset-based, partitioned, lineage-tracking) and **downgrades** Ray (batch-oriented, lineage at the object-store level not the per-item level).
It keeps Temporal and Restate strong for slightly different reasons than before — Temporal because Workflow IDs are the canonical idempotency key, Restate because Virtual Objects keyed by content-hash give you free per-key serialization.
Procrastinate's value goes up modestly (you can use `queueing_lock` for per-URL serialization) but it's still a task queue, not a workflow engine, so you build the firebreak yourself.
Absurd's step-checkpoint model maps cleanly onto the per-stage shape, but the maturity is much lower than the others.

### Comparison table — ingestion-optimized axes

| Axis | Temporal | Ray (Core/Data) | Procrastinate | Absurd | Restate |
|---|---|---|---|---|---|
| **Per-stage idempotency primitive** | Workflow ID dedup + activity-level idempotency key from `workflow_run_id + activity_id` (you write the table check) | None first-class; lineage recovery assumes deterministic tasks | `queueing_lock` (only-one-in-queue), `lock` (only-one-running); no result-dedup | `idempotency_key=` on `app.spawn(...)`; duplicate spawn returns existing task | `idempotency-key` HTTP header on invocations + Virtual Object key exclusivity |
| **Resumability mid-pipeline** | Replay from event history; activities not re-executed if completed | Re-execute lost objects via lineage from the last persisted upstream | Whole task re-runs from start (no step checkpoints) | Replay from last completed step checkpoint | Replay from last journaled step |
| **Conditional / branching** | Plain Python control flow inside workflow function | DAG built in driver code; conditional fan-out is awkward | Imperative; you chain by deferring next task at end of current | Plain Python control flow inside task function; `ctx.step()` per stage | Plain Python control flow inside handler |
| **Per-item granularity** | One workflow per URL; child workflow per chunk is the canonical pattern | Whole-batch dataset; per-item is a Ray task but no per-item state | One job per stage per item; you compose | One task per item; sub-tasks for chunks via `ctx.await_task_result` | One handler call per item; per-key Virtual Object queue |
| **GPU co-location** | Task Queue per resource class — explicitly documented for GPU vs non-GPU workers | First-class `num_gpus=` and actor placement; best-in-class | None — workers are uniform, you partition by `queues=[...]` | Workers consume from named queues; route by `queue=` on spawn | Different services on different deployments; you route by which Restate "service" handles which step |
| **Heterogeneous input dispatch** | `if` on input type at the top of the workflow → different activities | Driver code branches | Different task per type, deferred from a dispatch task | Different step or different child task per type | Different handler per type, dispatched from a router service |
| **Dedup / replay protection** | Workflow ID = content hash; `WorkflowIdReusePolicy=REJECT_DUPLICATE` on Temporal Server side | None | `queueing_lock=content_hash` ensures only one queued at a time | `idempotency_key=content_hash` returns existing task on duplicate spawn | `idempotency-key: <content_hash>` header → server returns first invocation's result |
| **Small-scoped reprocessing** | Start a new workflow scoped to chunk (e.g., `WorkflowID=chunk:<hash>:reextract:<v>`) | Recompute Dataset partition; coarse | Defer just the affected stage tasks for that chunk | Spawn a new task with new `idempotency_key` for that chunk | Invoke the per-chunk Virtual Object handler again; or click "Restart as new" in the Restate UI from any journal entry |
| **Backpressure / rate limit** | Task Queue rate limits server-side; activity-level concurrency | `max_concurrency` on actor pools; placement groups | Worker concurrency caps and per-queue workers; no token bucket | Worker pull rate; no first-class rate limit | Concurrency limits per service/handler; enqueue per virtual object key |
| **Operational footprint** | 4 services (Frontend, History, Matching, Worker) + Cassandra/Postgres + UI; or Temporal Cloud | Ray head + workers; cluster manager (KubeRay) | Postgres + worker process(es); ~minimal | Postgres + worker; single SQL file schema | Single binary (Restate Server) + worker process(es) |
| **Failure forensics** | Web UI with full event history, replay, query, signal | Ray Dashboard, decent for tasks/actors; less granular for pipeline stages | SQL on `procrastinate_jobs` + tiny CLI shell (`list_jobs`, `retry`); no UI | Habitat dashboard (read-only): queues, tasks, runs, checkpoints, events; per-attempt error details | Restate UI 1.5: live timeline, journal per invocation, "Restart as new" from any journal entry, SQL via DataFusion |
| **Pipeline-health observability** | OpenTelemetry + UI metrics; mature | Ray Dashboard + Prometheus | Roll-your-own from logs; structured logging present | None first-class for queue depth / latency distributions; instrument yourself | OpenTelemetry; UI metrics; SQL `sys_invocation` table for ad-hoc queries |

---

## Details

### Temporal

The canonical pattern is **`WorkflowID = "ingest:" + sha256(content)`** with a Reject-Duplicate reuse policy.
Temporal's docs are explicit: _"Use WorkflowIds as idempotency keys when Workflow may be started more than once."_
If the same URL is submitted twice, the second `start_workflow` call returns a duplicate error rather than a second execution.
Inside the workflow, each stage is an activity, and the canonical activity-level dedup pattern (from Temporal's Python error-handling docs) is to combine `workflow_run_id + activity_id` as an idempotency key and check a database row before doing the side effect:

```python
@activity.defn
async def graph_write_activity(payload):
    info = activity.info()
    key = f"{info.workflow_run_id}-{info.activity_id}"
    if existing := db.query(GraphWrite).filter_by(idempotency_key=key).first():
        return existing.result
    # do the actual Neo4j write, then insert the row
```

For per-chunk firebreak reprocessing, the natural pattern is a **child workflow per chunk** with `WorkflowID = "chunk:" + chunk_hash + ":v" + extraction_version`.
Temporal is explicit that child workflows can carry a one-to-one mapping with a resource using ID for uniqueness.
To re-run only chunk X stages 4–6, start a fresh child workflow with a new `WorkflowID` that scopes to chunk X and only encompasses those activities — nothing else moves.

GPU co-location is a strong fit.
Temporal docs call out the GPU case directly: _"Some Workers might exist on GPU boxes versus non-GPU boxes._
_In this case, each type of box would have its own Task Queue."_
You'd run a `gpu-extraction` task queue on the 4000-series nodes for GLiNER/GLiREL/ReFinED/embedding activities and a separate `cpu-io` task queue for Trafilatura/Playwright/Neo4j writes.
Workflows are defined once and dispatch each activity to the right queue.

The cost: four Temporal Service components plus Postgres, plus the per-workflow event-history limit of 50,000 events / 50 MB.
For thousands of small chunk-sized child workflows that limit is comfortable; for a giant single ingestion of one huge document with 50k chunks, you'd want to use Continue-As-New or batched child workflows.

### Restate

For ingestion, the strongest Restate pattern is a **Virtual Object keyed by content-hash**: every URL/PDF gets one virtual-object instance, and Restate guarantees _"only one handler executes at a time for a given key, ensuring data consistency and preventing race conditions without manual locking."_
That's the closest thing in this comparison to a free per-item exclusivity guarantee.
Combined with the HTTP header `idempotency-key: <content_hash>` on invocations, you get both: the second submission of the same URL is deduplicated by Restate Server, and even if two requests slip through, the Virtual Object's intrinsic per-key lock serializes them.

The pipeline shape maps very cleanly.
The router service dispatches by input type.
The per-content Virtual Object's `run` handler is the workflow body — it's a normal async function with `await ctx.run("step_name", lambda: do_work())` for each durable step.
Each `ctx.run` is a journaled checkpoint; on crash, replay skips already-completed steps.
For the per-chunk firebreak, you can either nest a chunk-level Virtual Object (keyed by `chunk_hash`) inside the document-level one, or call a separate `chunk` service from the document handler.

The Restate UI 1.5 is genuinely good for failure forensics: live timeline of journal entries, the ability to **Restart as new** from any journal entry (effectively "re-run from step 4"), and a DataFusion-backed `sys_invocation` SQL view you can query for stuck or stalled invocations.
Operational footprint is excellent — a single binary (Restate Server) plus your service processes.

GPU co-location works the same way Temporal handles it: you run separate service deployments for GPU steps and CPU steps, and the workflow handler calls them via durable RPC.
This is slightly less explicit than Temporal's task-queue model, but it works.

The Python SDK is current (`restate-sdk 0.17.1`, uploaded to PyPI on April 8, 2026).
The main caveats: Restate is younger than Temporal, and _"parallel tool calls aren't supported out of the box due to non-deterministic replay"_ — for ingestion, where you legitimately want to fan out embed + NER + RE in parallel for one chunk, you wrap them with Restate's promise combinators (`restate.gather` / `restate.select`) inside a single step.
Once you do that, the journaling is correct.

### Absurd

Absurd is now the simplest match for the per-stage shape, but the maturity gap is real.
The model is exactly: a task has a sequence of named steps, each step's return value is checkpointed in Postgres, on crash the task replays from the last completed step.
For ingestion, this maps directly: one task per URL, with `process-url`, `extract-markdown`, `chunk`, then a fan-out into per-chunk steps or per-chunk child tasks.

The idempotency story is good and explicit.
Per the Absurd Concepts docs (verbatim): **"When spawning tasks, you can provide an idempotency key.**
**If a task with the same key already exists on the queue, the existing task is returned instead of creating a new one."**
The Python SDK exposes `idempotency_key=` on `app.spawn(...)`.
Ronacher's "Absurd in Production" post (April 4, 2026) confirms the production usage pattern verbatim: _"All our crons just dispatch distributed workflows with a pre-generated deduplication key from the invocation._
_We can have two cron processes running and they will only trigger one absurd task invocation."_

For per-chunk firebreak reprocessing, Absurd's `ctx.await_task_result()` (added in `absurd-sdk 0.3.0`, released April 2, 2026) lets a parent ingestion task durably wait for child tasks, each with its own `idempotency_key=chunk_hash`.
The parent stays on a `coordinator` queue; chunk workers pull from a `gpu-chunk` queue (Absurd requires the child queue to differ from the parent's queue).

What Absurd does **not** have, per the April 2026 post: a built-in scheduler (_"There's no built-in scheduler._
_If you want cron-like behavior, you run your own scheduler loop and use idempotency keys to deduplicate"_); a push model (_"Everything is pull"_); and partitioning maturity (_"The biggest omission is that it does not support partitioning yet … the hard part is not partitioning itself, it's partition lifecycle management under real workloads"_).
For thousands of items per hour the pull model and Postgres-only architecture are fine, but the checkpoint table grows linearly without partition cleanup, which is non-trivial to operate at multi-million-task scale.
The Habitat dashboard (read-only, Go binary, served at `:7890` by default) shows queues, tasks, runs, checkpoints, and events with per-attempt error details — adequate for forensics, but it does not yet have per-step latency distributions or queue-depth gauges out of the box.

GPU co-location is per-queue: spawn extraction tasks onto a `gpu` queue served by GPU workers, write tasks onto a `cpu-io` queue served by CPU workers.
Same shape as Procrastinate; cruder than Temporal/Restate.

### Procrastinate

Procrastinate is a Postgres-backed task queue, not a workflow engine.
Two of its features map onto the ingestion problem:

- **`queueing_lock="ingest:" + content_hash`** prevents two ingestions for the same URL from queuing concurrently — `defer` raises `AlreadyEnqueued` for the duplicate.
  This is per-URL exclusivity, and it's a reasonable pattern, **not** an abuse.
- **`lock="ingest:" + content_hash`** prevents two jobs with the same lock from running concurrently — Procrastinate guarantees in-order, one-at-a-time execution for jobs sharing a lock.

What's missing is the per-stage checkpoint.
There is no `ctx.step()`, no event history, no journal.
If the embedding stage succeeds but the graph-write stage crashes, Procrastinate has no notion that embedding is done — when the job is retried it re-runs from the top of the task function.
The discipline you need is to either (a) make every stage write its result to a content-addressable store keyed by `(content_hash, stage_name)` and check that store at the top of each stage, or (b) split the pipeline into a chain of separate Procrastinate tasks, each one deferring the next at the end of its own success — which gives you stage-level checkpointing for free, at the cost of carrying state through Postgres.

A real gotcha worth flagging: **if a worker dies while holding a `lock`, that lock can stay stuck in `doing` forever.**
 Per the LeanIX engineering blog (verbatim): _"If a job with XXXXXXXXXXXXX is being processed and the worker dies, it's stuck in that state and no other job with that lock can be started._
_We added a periodic task that checks for these stalled jobs and resets them to todo."_
Procrastinate has a `JobManager.get_stalled_jobs()` plus a documented periodic-task pattern (`@app.periodic(cron="*/10 * * * *")`) to retry stalled jobs by detecting workers whose heartbeat has expired (default 30 s) — you must wire this up yourself, it is not on by default.

For GPU routing, you partition queues (`@app.task(queue="gpu-embed")`) and run dedicated workers.
Failure forensics is by SQL on `procrastinate_jobs` + the tiny `procrastinate shell` CLI; no real UI.

### Ray

Ray Data's lineage-based recovery is genuinely elegant — when an object is lost from the distributed object store, Ray can reconstruct it by re-running the producing task — but the Ray docs themselves note the limitations (verbatim from the Ray 2.54.1 fault-tolerance page): _"Tasks are assumed to be deterministic and idempotent."_
Most ingestion stages aren't deterministic in the strict sense (LLM enrichment, network fetches, ML inference with non-deterministic ordering), and the recovery boundary is the object, not the stage.
There is no first-class "this URL was already ingested, skip it" primitive at the orchestrator level.

What Ray is genuinely best at, and where it should still be in the architecture: **GPU model serving and batch embedding**.
Ray Serve / Ray Data with `num_gpus=` per actor, autoscaling actor pools, vLLM integration, and explicit support for "CPUs and GPUs in the same pipeline to increase utilization, fully saturate GPUs, and decrease costs" makes it the right tool for hosting the GLiNER/GLiREL/ReFinED/embedding models behind the orchestrator — but **not** for orchestrating the per-document pipeline.
Use Ray Serve as a model-serving sidecar that the actual orchestrator (Temporal/Restate/Dagster/Absurd) calls into; do not use Ray Core as the durable executor.

### Adjacent tools that may actually fit better

The reframe deserves an honest look at tools that weren't in the original five.
Three of them genuinely belong in the conversation.

**Dagster — strong contender, may be the right answer.**
If you accept that this is a **data pipeline** (assets, lineage, partitions, backfills) and not a **durable workflow** (control flow, signals, agent loops), Dagster is the most ingestion-native tool of any in this comparison.
The model is exactly right:

- The Neo4j knowledge graph is a set of **assets**: `chunk_embeddings`, `entities`, `entity_links`, `relationships`, `enriched_chunks`, `graph_writes`.
  Each asset has typed dependencies on upstream assets.
- Each chunk becomes a **dynamic partition** (`DynamicPartitionsDefinition`).
  Dagster's docs make this explicit (verbatim): _"If certain files contained corrupted data, you can reprocess just the affected files._
  _If there was an error in your processing logic, you can run a backfill that re-processes all your files."_
- Idempotency is the natural pattern: every asset materialization for a partition first deletes existing data for that partition then writes fresh — Dagster's tutorials use the phrase verbatim: _"This keeps the asset idempotent."_
- Per-chunk firebreak reprocessing is a one-line operation: select the partition for chunk X in the UI and click Materialize, or call the equivalent in code.
  Nothing else cascades.
- Asset-aware lineage and per-partition status are first-class in the UI — the prompt's "failure forensics" axis is essentially what Dagster was built for.

The trade-offs that keep Dagster in 3rd place rather than 1st: (a) it's a heavier deployment than Restate or Absurd (Dagster webserver, daemon, code locations, plus a metadata DB) — comparable to Temporal in operational footprint; (b) it's optimized for batch and incremental processing, not for sub-second per-item latency; (c) per-partition runs have non-trivial overhead — a Dagster maintainer in GitHub issue dagster-io/dagster #18190 acknowledged that _"I don't think in general 2 minutes startup time is expected in kubernetes"_ in response to a user reporting ~2-minute per-run startup overhead in Kubernetes hybrid deployments, and a Dagster Slack thread (discuss.dagster.io/t/8143251) records the same ballpark cold-start cost without disputing the figure.
For a weekly/hourly batch ingestion of thousands of items, this is fine; for "user pastes a URL and wants to see it in their graph in 10 seconds," Dagster will feel sluggish unless you move the synchronous path off Dagster and only use Dagster for backfills and batch reprocessing.

**Honest assessment:** if your ingestion is mostly batch-oriented, Dagster is probably the right answer and is a better fit than any of the original five.
If you have a low-latency interactive path (someone adds a URL and expects to see it ingested in seconds), keep Dagster for batch reprocessing/backfills and use Temporal or Restate for the interactive path.

**Hatchet — drop-in alternative, similar shape to Temporal.**
Hatchet positions itself as a Postgres-native durable-task framework with a "drop-in replacement for Temporal or DBOS workflows."
For ingestion specifically, the relevant features are durable tasks with checkpointed event log and resumability after eviction; **rate limiting** with static and **dynamic** keys (CEL expression on input — e.g., `rate_limits=[RateLimit(dynamic_key='input.user_id', units=1, limit=10, duration=RateLimitDuration.MINUTE)]`) which is a clean fit for OpenRouter rate-limit isolation per user; **concurrency control** with `GROUP_ROUND_ROBIN` and `CANCEL_IN_PROGRESS` strategies keyed by input fields; and a **Postgres-only operational footprint** (vs. Temporal's four services).
Hatchet does not have the "Workflow ID dedup" guarantee Temporal has at the server level, but it has decent retry semantics and explicit idempotency design guidance.
If the four-service Temporal footprint is the deal-breaker but you want most of Temporal's mental model, Hatchet is the right drop-in.
It is younger, smaller community, and the dynamic-rate-limit feature is the standout for the OpenRouter case.

**DBOS — narrowest, simplest, weakest UI.**
DBOS is closest to "a library you import" rather than "infrastructure you run."
Workflows are decorated functions; steps are decorated functions; state is in Postgres; `SetWorkflowID(content_hash)` is the canonical idempotency-key pattern (verbatim from DBOS docs): _"An assigned workflow ID acts as an idempotency key: if a workflow is called multiple times with the same ID, it executes only once."_
For a small team with an existing Postgres database, DBOS gives most of Temporal's resilience semantics with a fraction of the operational footprint.
The trade-off: DBOS workflows must be deterministic in the same way Temporal workflows must be — non-deterministic operations have to be wrapped in `@DBOS.step`.
The UI/forensics story is much weaker than Temporal/Restate/Dagster (DBOS Conductor exists but is a paid hosted product).
For a research-style ingestion pipeline where forensics matter, this is a real cost.

**Inngest, Trigger.dev, Cloudflare Workflows.**
Inngest is event-driven and TypeScript-first; the `idempotency:` field on `createFunction` (CEL expression) is genuinely clean for content-hash dedup — `idempotency: 'event.data.content_hash'` is exactly the pattern — but Inngest is a managed service or a separate self-hosted dev server, the Python SDK is much less mature than the TS one, and the **24-hour idempotency window is a hard product limit** (Inngest docs: _"Event IDs will only be used to prevent duplicate execution for a 24 hour period"_), which is wrong for content-hash dedup that should hold forever.
Trigger.dev is TypeScript/Node-first; the `idempotencyKey` on `tasks.trigger()` plus `idempotencyKeys.create(hash(payload))` is a clean pattern, but the platform is centered on managed cloud and skip for a Python-native ML stack.
Cloudflare Workflows / Workers Workflows doesn't make sense for a self-hosted GPU + Neo4j stack.

**Windmill, Prefect, Airflow.**
Windmill has first-class "workflows as code" in Python and TypeScript with explicit data-pipeline patterns (`step()` for checkpointed inline values, `task()` and `taskFlow()` for jobs, `sleep()`, `wait_for_approval()`); it claims faster step overhead than Airflow/Prefect/Temporal.
Strong dark horse, though the asset-lineage story is much weaker than Dagster's.
Prefect v3's task `cache_policy=INPUTS` plus result persistence gets you content-hash idempotency cleanly: same inputs → cached result returned.
The flow-level idempotency story is weaker (it's an open feature request — PrefectHQ/prefect issue #7288), and Prefect doesn't have asset-lineage like Dagster does.
If you're already on Prefect, fine; if you're choosing fresh, prefer Dagster for ingestion.
Airflow is the wrong tool — DAG static, dataset features are improving but task-startup overhead is too high for per-item ingestion.

---

## Recommendations

Concretely, given the existing architecture (Neo4j CE + n10s, Trafilatura/Playwright, fastcoref → GLiNER → GLiREL → ReFinED → nomic-embed, k3s GPU cluster, OpenRouter):

1. **Temporal** — best overall fit if you can tolerate four services.
   Workflow IDs as content-hash keys give you server-side dedup, child workflows give you per-chunk firebreaks, multiple task queues give you GPU/CPU routing, and the UI gives you failure forensics out of the box.
   The Python SDK is mature, the activity-level idempotency pattern is well-documented, and the event-history limits won't bite a per-document workflow.
   **Switch from this pick if** you can't run four services, in which case go to (2) or (5).

2. **Restate** — best fit if footprint matters.
   Single binary, Virtual Objects keyed by content-hash give you per-key exclusivity for free (no `lock` table to maintain), the `idempotency-key` HTTP header gives you server-side dedup, and the UI 1.5 is the only tool in this list with "Restart as new" from any journal entry — which is the cleanest way to do "re-run stages 4–6 for chunk X." Younger and smaller community than Temporal.
   **Switch from this pick if** the Python SDK feels too thin for your team or if parallel-fan-out-per-chunk under one journal becomes painful.

3. **Dagster** — best fit if you accept this is a data pipeline.
   Dynamic partitions per chunk are exactly the firebreak primitive; asset-aware lineage gives you forensics that no other tool here matches; idempotent partition materialization is idiomatic.
   Use this if your ingestion is mostly batch and you don't need sub-second interactive ingestion.
   **Switch from this pick if** you need interactive end-to-end ingestion under ~30 seconds — Dagster's per-run startup overhead can dominate.

4. **Absurd** — best fit if simplicity dominates.
   Postgres only, `idempotency_key=` on spawn is clean, step-checkpoint model maps directly onto your stage shape.
   Real caveats: no built-in scheduler, no push model, partitioning is opt-in but lifecycle management is non-trivial, observability is basic.
   Reasonable choice for a small team that's already on Postgres and wants the smallest possible footprint, but bet on the project's maturity carefully.
   **Switch from this pick if** you exceed ~hundreds-of-thousands of tasks per day (partition-cleanup operational cost), or if missing per-step latency observability blocks debugging.

5. **Hatchet** — strong dark horse if you want Temporal's model with Postgres-only footprint.
   Best dynamic-rate-limit story for the OpenRouter case.

6. **Procrastinate** — fine as a per-task queue, the firebreak and step-checkpointing are bolted on by hand.
   `queueing_lock` is a legitimate per-URL exclusivity primitive.
   Don't expect to graduate this into a full workflow engine.
   **Wire up the stalled-jobs periodic task** before going to production or you will deadlock yourself.

7. **Ray** — keep it, but as a Ray Serve model-serving layer behind whichever orchestrator you choose, not as the orchestrator itself.

### Specific patterns

**Per-stage idempotency:**

- _Temporal:_ `WorkflowID = "ingest:" + sha256(canonical_content)`.
  Inside the workflow, every activity uses an idempotency key composed of `workflow_run_id + activity_id`, checks a `processed_keys` table before doing the side effect, and inserts the row in the same DB transaction as the result.
  For Neo4j writes, use Cypher `MERGE` keyed on the chunk's content hash to make the write idempotent at the database level too.
- _Restate:_ Send the invocation with `idempotency-key: <content_hash>` header.
  Inside the handler, every durable step is `await ctx.run("step_name", lambda: do_work())`.
  The journal is the source of truth.
- _Absurd:_ `app.spawn("ingest", payload, idempotency_key=content_hash)`.
  Inside, `@step("extract-markdown") def extract(): ...`.
  Each step's return is checkpointed.
- _Procrastinate:_ `task.configure(queueing_lock=content_hash, lock=content_hash).defer(...)`.
  For per-stage idempotency, write each stage's output to a content-addressable store and check it at the top of each stage.
- _Dagster:_ Define each stage as an asset, partition by chunk hash, and make every materialization "delete-then-insert" for that partition.

**Content-hash workflow IDs.**
The right hash is over canonical content, not over the URL.
URLs change; content reflows; you want to re-ingest if the page changed.
So: `sha256(normalized_extracted_text)` after Trafilatura/Playwright extraction, scoped by extractor version.
A reasonable scheme is `sha256(extractor_version + ":" + extracted_text)`, then use that as the workflow ID / idempotency key.
If extracted text is the same as last time, the second submission is a no-op; if it changed, you get a fresh workflow.
For per-chunk IDs, use `chunk_hash = sha256(chunk_text)` and key chunk-level work by that.

**Conditional branching.**
Temporal/Restate/Absurd all support branching via plain Python control flow inside the workflow function.
In Temporal, an `if` on the output of an extraction-confidence activity decides whether to call `escalate_to_llm_activity`.
Same in Restate and Absurd.
In Procrastinate, you accomplish this by deferring different next-stage tasks at the end of the current stage.
In Dagster, this is awkward — assets are static — so you use sensors or asset checks to trigger the optional path.
Expressivity ceiling is highest for Temporal/Restate/Absurd; lowest for Dagster.

**Per-chunk firebreak reprocessing:**

- _Temporal:_ start a new workflow with `WorkflowID = "chunk:" + chunk_hash + ":reextract:v" + version`, scoped to only the affected stages.
- _Restate:_ invoke the chunk Virtual Object's handler again with a new idempotency key (e.g., suffix with the new version).
  Or use the UI's "Restart as new" from the relevant journal entry.
- _Absurd:_ `app.spawn("chunk-reextract", ..., idempotency_key=chunk_hash + ":v" + version)`.
- _Procrastinate:_ defer the affected stage tasks for that chunk; lock keyed by chunk hash prevents double-runs.
- _Dagster:_ materialize just the dynamic partition for chunk X. Click in the UI; everything downstream auto-materializes if you've configured automation conditions.

**GPU co-location:**

- _Temporal:_ task queue per resource class. `gpu-extraction` and `cpu-io` task queues, separate workers, workflows pick the queue per activity.
- _Restate:_ separate service deployments.
  The orchestrator service running on CPU calls into the GPU service via durable RPC.
- _Absurd / Procrastinate:_ named queues; spawn/defer with the right queue, run dedicated GPU and CPU workers.
- _Dagster:_ asset-level resources / op tags drive scheduling onto the right Kubernetes pod template.
- _Ray (as sidecar):_ Ray Serve actors with `num_gpus=0.5` (or whatever fraction) for GLiNER/GLiREL/ReFinED/embedding; the orchestrator just calls the Ray Serve HTTP endpoint.

---

## Caveats

- The "best for ingestion" answer depends on whether your ingestion is interactive (a single URL ingests in seconds) or batch (thousands of items per hour, latency irrelevant).
  Temporal/Restate are better for interactive; Dagster is better for batch.
- The 24-hour idempotency window in Inngest is a **product choice**, not a fundamental limit, and it disqualifies Inngest for content-hash dedup that should be permanent.
- Absurd is genuinely young: the original announcement post is dated November 3, 2025 (Armin Ronacher, lucumr.pocoo.org), the project has been "in production for five months" as of April 2026, and the GitHub repo (`earendil-works/absurd`) shows ~1,725 stars as of May 2, 2026.
  The technical fit for ingestion is excellent but the maturity bet is real.
  Specifically: no built-in scheduler, no push model, partitioning lifecycle is operationally tricky, no per-step latency observability.
- Procrastinate's stalled-job recovery is **opt-in** — you must wire up the periodic `retry_stalled_jobs` task yourself or stuck `lock`-holding jobs will deadlock other ingestions for the same URL.
- Ray's lineage recovery, while elegant, **assumes deterministic and idempotent tasks** (Ray docs, verbatim).
  Most ML inference and LLM enrichment stages are not strictly deterministic.
  Don't treat Ray Core's fault tolerance as a substitute for application-level idempotency keys.
- Restate parallel fan-out within a step requires using Restate's promise combinators rather than naive `asyncio.gather`, because of replay determinism.
  For chunk-level fan-out (embed + NER + RE + linking in parallel), this is a real but minor refactor.
- All recommendations assume the ML model stack (fastcoref, GLiNER, GLiREL, ReFinED, nomic-embed) is hosted behind a stable RPC interface — Ray Serve is the strongest option for this regardless of which orchestrator you pick.
  Hosting these as in-process imports inside Temporal activity workers / Restate handlers / Absurd workers / Dagster ops also works but limits scaling flexibility.
- Whichever tool you pick, **the Neo4j write itself must be idempotent** — Cypher `MERGE` on chunk content hash, plus `MERGE` on entity canonical-id, plus `MERGE` on relationship `(src, rel_type, dst, chunk_id)`.
  Orchestrator-level idempotency is necessary but not sufficient; the database-level idempotency is the last line of defense and the cheapest place to make double-writes impossible.
- This report was written on May 8, 2026 against the latest published documentation and blog posts available; tooling here is moving quickly (Absurd had a major release ~5 weeks before this report; Restate UI 1.5 and Restate Python SDK 0.17.1 are recent).
  Re-check status of partitioning in Absurd and parallel-fan-out support in Restate before committing.
