# Working Note: Orchestration-Flex Exploration

> **Status:** Architectural exploration / ideation. **Revised 2026-05-26 after a triple review** — a falsification/pressure-test pass, an adversarial steelman + what-if pass, and an author adjudication of both. No spec changes proposed. One concrete code bug surfaced and routed to [`tasks.md`](tasks.md) (W1, below).
> **Scope:** Can the `aizk.pipeline` abstractions (current + in-flight `pipeline-stage-runtime`) flex to other orchestrators — roll-our-own, honker, procrastinate, absurd, restate, prefect, windmill — without a rewrite? What do industry Temporal usages teach us?
> **Relationship to ADR-009:** Refines, not replaces, [009-orchestration](../../../docs/decision-record/009-orchestration.md). Adds procrastinate + restate (not currently in the ADR) and a portability lens over the in-flight runtime.
> **What the review changed:** the run primitive is portable in _shape_ but **not "untouched"** (W1 — a real dialect bug); the determinism constraint lands on the _discarded_ runner, not on `execute` (which strengthens, not weakens, the thesis); the `StageHandler` "two clean ports" split is a first cut with straddlers; the "3-band gradient" is three real clusters but **not a path you climb**; and the likely migration _trigger_ is **capability primitives, not durability granularity**. Recommendations collapsed from three moves to one (reframed) + one ADR edit.

## Sources read

- Phil Calcado, _Building AI Products — Part I: Back-end Architecture_ (microservices→objects; CQRS/event-sourcing for agents; adopting Temporal when hand-rolled durability/backpressure became untenable — the trigger was **capability** [signals, HITL, long waits], not throughput).
- Phil Calcado, _Part II: Task-Oriented vs. Component-Oriented Pipelines_ (big-verb tasks as self-contained units; agent-as-parent-workflow, tasks-as-subworkflows on Temporal).
- Rapidflare, _Scalable Ingestion Pipeline with Temporal_ Parts 1 & 2 (workflow/activity split; page-based resume + constant-size state; sliding-window fan-out; layered retries; status-returns-over-exceptions; two-version data swap; dedicated status worker as a durable read-model; three-level correlation IDs).

## Reframing the question

The `StageHandler` protocol plus the (still-unbuilt) runner **are a roll-our-own orchestrator.**
The runner owns exactly the responsibilities an engine takes over: claim/lease, bounded concurrency, drain, cancel, timeout, stale-recovery, retry scheduling.
So "can our abstractions flex to those tools" really means:

> Which parts of `aizk.pipeline` are **engine** (replaced by adopting one of these) and which are **domain** (survive any engine)?
> Flex = a fat, portable domain core behind a thin, swappable engine seam.

This aligns with Calcado Part II: _task-oriented_ stages — self-contained "big verbs" (Contextualize, ExtractMentions, Canonicalize) with clean I/O contracts and no hardcoded assumptions about order/failure — are what drop into a Temporal subworkflow later; _component-oriented_ sequentially-coupled pipelines do not.

## Three clusters — real categories, **not** a gradient you climb

| Cluster                            | Durability unit          | Re-run on crash                | Determinism req              | Ops footprint           | Tools                                                          |
| ---------------------------------- | ------------------------ | ------------------------------ | ---------------------------- | ----------------------- | -------------------------------------------------------------- |
| **A. Job-row queue**               | a job row                | whole task re-runs             | none                         | embedded / DB-only      | **roll-our-own (us)**, honker (SQLite), **procrastinate** (PG) |
| **B. Checkpointed task/DAG**       | a step result            | resume at last step            | none                         | DB-only → server+DB     | **absurd** (PG), prefect                                       |
| **C. Journaled durable execution** | a journaled `ctx` action | replay journal, re-run nothing | **yes** (orchestration code) | single binary → cluster | **restate**, temporal; **windmill (workflows-as-code)**        |

We sit in cluster A, embedded SQLite.

**Why these are clusters, not a ladder (review correction 3b):** the columns are _independent axes_ — durability granularity, determinism (a binary, C-only property — not a gradient), and ops footprint — projected onto one line.
The projection misleads: ops cost is **not monotonic** (honker is cluster A but embedded; restate is cluster C but a single binary), and cluster letter **anti-correlates** with migration desirability — the cheapest valuable move from where we are is A→A (honker, same file, maintained primitives), and the most _capable_ affordable move is A→C (restate, single binary, signals/long-waits).
You don't climb A→B→C; you pick the _cheapest tool that provides the primitive you need_, at any cluster.

Windmill straddles B/C: its visual flows are checkpointed-DAG (no determinism), but its _workflows-as-code_ replay from checkpoints and **require deterministic orchestration** like Temporal — so it can't be a single clean cell.
ADR-009 already relegates Windmill to operator-UI, so this is inert here.

Flex verdict per move:

- **A→A** (honker, procrastinate): **cheap** — reimplement work-discovery/claim/lease/stale-recovery; domain core untouched (modulo W1). honker is cleanest: same SQLite file, so the same-transaction co-commit invariant _survives literally_ (caveat: honker is **alpha** — no `:memory:`, a transaction pins a connection mutex; both bite our test setup).
  Note procrastinate does **not** ship stale/zombie-job recovery — you hand-roll the sweep (correcting an earlier overstatement that "they all do stale-recovery").
- **A→B** (absurd, prefect): runner discarded; `execute` decomposes into the engine's steps; `map_result` maps to retry config.
  Run-primitive + event-log survive (modulo W1).
  Authoritative status moves toward the engine; our event row becomes a projection.
- **A→C** (restate, temporal): runner gone; `execute`/`map_result`/`cleanup` → activities; orchestration → deterministic `ctx`-mediated code.
  Domain core survives.
  **The determinism constraint lands on the orchestration we _discard_, not on `execute`** — in C engines, side effects live in activities/`ctx.run`, which carry no determinism requirement (only idempotency-under-replay).
  So a C move needs **no per-`execute`-body audit** (correcting this note's earlier claim); it relaxes the cross-store co-commit (see `PipelineEvent` below).

## The axis that actually triggers migration: **capability, not durability** (review insight 2a)

The cluster table measures _durability granularity_.
But Calcado Part I's real trigger for adopting Temporal was **capability**: signals, human-in-the-loop, await-on-external-event, multi-day sleeps.
Those are **orthogonal to the clusters** and live in the **orchestration/engine layer** — the exact layer this note otherwise calls disposable.
Implication that reframes the whole exercise:

- Hardening the _portable domain core_ does **not** help with the most plausible future need.
  The need would be a _new primitive_, and primitives are engine-owned.
- And critically: **"we need a primitive" ≠ "we must leave SQLite."** honker (cluster A, same file) already offers pub/sub + durable event-await; absurd (cluster B, Postgres) offers event-await + sleep.
  So a capability trigger may be satisfied _in place_, not by a Temporal-scale migration.
- Durability granularity is still a _real_ secondary axis (step-checkpointing matters for an expensive LLM contextualization run you don't want to redo from scratch) — but it's not the likely _trigger_, and this note over-indexed on it.

## Per-abstraction verdict: what ports, what is welded

| Abstraction (file)                                  | Verdict                                                                     | Why (post-review)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| --------------------------------------------------- | --------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `PipelineRun` / `record_run` (`run.py`)             | **Portable in shape — but NOT "untouched" (W1 bug)**                        | Dataset-versioning/lineage; no orchestrator provides it (it is Rapidflare Pt2's two-version swap). It survives an engine swap as "a transactional DB op a stage performs," never "a runner step" — design already separates it. **But the one-active-run invariant is enforced by a partial index declared `sqlite_where`-only ([run.py:62](../../../src/aizk/pipeline/run.py#L62) + migration), which Postgres silently drops** → degrades to a full unique index → forbids superseding. So it _breaks_ on the very Postgres engines (procrastinate/absurd) ranked as cheapest. Fix routed to `tasks.md` (W1).                        |
| `WorkUnitStatus` + `RetryClass` (`lifecycle.py`)    | **Portable — the lingua franca** ✅                                         | `{succeeded, failed, cancelled, timed_out}` + retryable/permanent is the lowest common denominator every engine expresses (Temporal `non_retryable`; Restate `TerminalError` vs `Exception`; procrastinate `retry_exceptions`+`max_attempts`). `map_result` is the translation seam. The _consumer_ (retry-wait eligibility) is engine territory — keep it in the runner, never the adapter.                                                                                                                                                                                                                                           |
| `PipelineEvent` / `record_transition` (`events.py`) | **Portable as a read-model; the co-commit _mechanism_ shifts in cluster C** | The table is Rapidflare Pt2's "dedicated status worker" — even Temporal shops re-project lifecycle into their own DB keyed by _their_ identity (our `aizk_uuid`). Not redundant under an engine; it is the thing you build anyway. Precise correction: co-commit **within your DB** survives in C (an activity opens a txn, writes row + event, commits). What you lose is **cross-store atomicity** between your DB and the engine's progress marker — the activity may commit then crash before reporting success, so the write must be **idempotent under replay**; the row demotes from "authoritative" to "idempotent projection." |
| `StageHandler` (`repository.py`)                 | **Mixed — a first-cut split, with straddlers**                              | First cut: domain (`validate_dependencies`, `execute`, `map_result`, `cleanup`, `scope_key`) vs queue (`claim_next`, `recover_stale`, `finalize`). But several straddle: `finalize` and `claim_next` each contain a _claim/lease_ half (engine-replaced) **and** a _status-transition write_ half (the projection that survives); `concurrency_limit`/`timeout` are stage-_declared_ but engine-_consumed_; `cancel` is meaningful only relative to who owns the run loop. So design.md's "the seam splits cleanly" is overconfident. The precise line is below, not method-by-method.                                                  |
| Runner (unbuilt)                                   | **This _is_ the engine — swappable wholesale** ✅                           | Best news: it doesn't exist yet. Define it as _one implementation of an engine role_, bound to adapters at the composition root, calling only the domain operations.                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |

### The precise portability line (correcting both this note and the reviewer)

Not "which method."
The engine-replaced part is specifically **work discovery → claim → lease → eligibility-ordering → stale-recovery** (the SQL-queue logic an engine does itself).
The **transactional table operations** — `record_run`, `record_transition`, and `finalize`'s status-write — are **caller-agnostic**: their "caller owns the session, helper never commits" convention is _portable_ (the caller is the runner today, an activity/step tomorrow).
So `claim_next`'s eligibility-SELECT dies; `claim_next`'s/`finalize`'s _transition write_ survives as the projection.
All three primitives sit on the **same** session/co-commit mechanic — which is why rating one "dead" and another "fully portable" was internally inconsistent (review I3).
They port together; only the _discovery/claim logic_ is welded.

## What the documents teach (mapped to us)

1. **Constant-size orchestration state / DB-as-queue** (Rapidflare Pt1): our `claim_next` already makes the DB the source of truth and the runner hold only bounded in-flight state.
   Keep it — it is what makes a future `continue-as-new`/engine port painless.
2. **Bounded fan-out beats unbounded** (Pt1): treat per-stage `concurrency_limit` as a _rate_ knob (Little's Law), not just a resource cap — directly relevant to the LLM contextualization stage.
3. **Retry at exactly one layer** (Pt2): contextualization/extraction will have _provider-level_ retry; the runner must not re-retry on top. `map_result` owning retryable/permanent is the seam that lets a stage say "do not retry me."
4. **Unit-level failure is data, not control flow** (Pt2): `map_result(result_or_exc)` accepting _both_ result and exception already supports this — permanent(domain) vs retryable(infra) is the right cut.
5. **Durable read-model decoupled from the engine** (Pt2): the strongest validation that `pipeline_events` survives a swap — _never let the operator UI query the engine; go through our own projection._
6. **Three-level correlation IDs** (Pt2): make the structured-logging requirement _name_ `(run_id, aizk_uuid, stage, work_unit_ref)` so cross-stage + cross-restart debugging works on any engine.
7. **Config-refresh-per-run** (Pt2): if scheduled reprocessing arrives, refresh config at _run_ start; `validate_dependencies` is today's analog.
8. **The migration trigger is _capability_, not durability** (Calcado Pt1): adopt a primitive when the job-table cannot express signals / human-in-the-loop / event-await / long waits — and prefer the cheapest tool that provides it _at any cluster_ (honker satisfies several in-place).
   ADR-009's rationale still holds; this sharpens its trigger from "complexity" to "named primitive."

## What to actually do (collapsed after review)

The triple review shrank the original three "cheap moves" hard:

1. **Keep `execute`/`map_result` free of `Session`/claim semantics — justified by _present-tense testability_, not migration insurance.**
   This is already the design's functional-core/imperative-shell intent ("stages testable without the runner"), so it is not new work — just don't regress it.
   _Reframed_ from the original "document the swap line": the value is testing today; the portability is a free side effect.
2. **(Retracted) ~~Reword the event-log spec requirement as engine-neutral~~.**
   This would _dilute_ a crisp, testable, currently-true invariant ("co-committed in the same transaction") into a hedge against a migration that probably won't come, and it would smuggle a band taxonomy into a spec requirement (against the "specs stand alone" stance).
   Drop it.
3. **(Downgraded) Naming the runner an "engine" at the composition root** carries a misuse-attractor risk — it advertises a second-engine seam toward the "meta-engine over all clusters" trap the design explicitly fears.
   Optional cosmetic; not recommended on its own.
4. **(ADR, 2 sentences) Add restate + procrastinate to ADR-009's alternatives** — restate is single-binary cluster-C durability **below** Temporal's _starting_ ops cost (HA still wants a cluster + object store); procrastinate is a mature cluster-A Postgres queue _minus_ step-checkpointing and _minus_ free stale-recovery.
5. **(Code) Fix W1** — see `tasks.md`.
   The "≤1 active per `(stage, scope_key)`" requirement is dialect-agnostic; the implementation accidentally pinned it to SQLite.

Already right — keep: run-primitive separated from execution; event log keyed by `aizk_uuid`; generic lifecycle as translation layer; per-stage concurrency; composition-over-inheritance; explicitly "primitives, not a framework."

## Pointed takeaways — runtime-side mechanisms that MUST hold

The load-bearing contracts this note reduces to (mechanisms, not named structures).
All but the last two are **already requirements in the pipeline-stage-runtime spec** — listed so they are not regressed during the runner build and conversion port.

- **Functional core / imperative shell.**
  The unit-of-work (do-the-work + classify-outcome) is separable from the I/O shell (claim/commit/transition/cleanup); the runner owns the DB session/transaction, the work logic owns neither.
  This is simultaneously today's testability and tomorrow's engine-portability.
  **[locked]**
- **Generic lifecycle is the lingua franca.**
  Stages map their statuses onto queued→running→{succeeded, failed, cancelled, timed_out} + retryable/permanent; the runner reasons only over the generic set.
  Retry-wait eligibility is the runner's, never the adapter's.
  **[locked]**
- **A status change and its transition event co-commit atomically**, or neither persists. **[locked]**
- **The event log is a durable read-model keyed by source identity** — built regardless of engine; an operator/query surface reads the projection, never the engine's own history. **[locked + discipline]**
- **Retry at exactly one layer.** map_result owns retryable/permanent so a stage with provider-level retry can opt out of runner retry. **[discipline]**
- **Keep the engine-replaceable seam narrow** (discipline, not a spec requirement): only discovery → claim → lease → eligibility → stale-recovery is engine-coupled; the transactional table ops and the unit-of-work stay caller-agnostic, so the runner can be swapped without touching stages. **[discipline]**

## Triple-review adjudication (what I accepted vs rejected)

**Accepted** (changed the analysis): W1 dialect bug (verified in code); determinism lands on the discarded runner not `execute` (O1, strengthens the thesis); `StageHandler` split is a first cut with straddlers (I1); co-commit = cross-store atomicity + idempotent projection (I2); the portability line is queue-discovery-logic vs transactional-ops, not whole-method (I3, refined); procrastinate has no free stale-recovery; the migration trigger is capability not durability (2a); the gradient flattens orthogonal axes — clusters real, path framing wrong (3b); "cost ≈ nothing" was false → moves collapsed; spec-reword move retracted (2c).

**Rejected / moderated** (reviewers overreached):

- **"The exploration itself was wasted budget."**
  Rejected — it was a commissioned ideation; judging it by "produced no code" misreads the brief.
  The legitimate kernel (novel content is small) is handled by compressing, above.
- **"Adopt honker _now_ instead of building roll-our-own."**
  Good question, wrong answer: `pipeline-stage-runtime` is a _behavior-preserving extraction of conversion's already-tested harness_, not a greenfield; honker is **alpha** (the same review flagged the `:memory:`/mutex hazards).
  Swapping tested code onto an alpha dep contradicts the project's risk posture; ADR-009 already names honker as the _future_ in-place upgrade.
- **"Self-contained tasks obstruct a cross-stage saga."**
  Moderated — task-orientation is generally _pro_-saga (each idempotent stage is a clean compensable step).
  The smaller real tension ("no universal work-unit table" complicates cross-stage _queries_) is already answered by `pipeline_events` keyed by `aizk_uuid`.
- **Windmill mis-banded.**
  Accepted as fact, downgraded as inert (ADR-009 already scopes Windmill to operator-UI).

**Honest meta-finding:** the original note was ~60% confirmation-of-existing-design dressed as analysis.
The durable signal is narrow: the **W1 bug**, the **restate gap** in ADR-009, and the **capability-vs-durability** reframe.
The three clusters are real; the "gradient you climb" framing was the main thing worth deleting.
