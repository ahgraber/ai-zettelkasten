# Working Note: App-Wide Architectural Shifts the Pipeline Patterns Expose

> **Status:** Architectural exploration / ideation — **no code or spec changes proposed here.**
> **Date:** 2026-05-26. **Revised twice on 2026-05-26**: first after cross-checking ADR-005/006, then after a second subagent pressure-test that corrected several factual claims and demoted the recommendations.
> **Origin:** Companion to [orchestration-flex-exploration.md](orchestration-flex-exploration.md). That note was pipeline-runtime-scoped; this one zooms out to the whole application.
> **Grounding:** Read directly — ADR-003 (database/write-topology), ADR-004 (model provider), ADR-005 (chunking), ADR-006 (graph construction), ADR-007 (embedding), ADR-008 (index/search/retrieval) — plus an implementation map of `src/aizk/`.
> **The fact that reframes everything:** the app is today **~100% ingest/derivation, 0% query** (grep confirms: no index/embedding/retrieval code exists). So the useful question is not "what read-side architecture to build" (premature) but "which write-side decisions being made _now_ will the eventual read side depend on."
> **Honest framing (after the second pressure-test):** this note is **largely confirmation** of ADR-003/005/006, plus **three genuinely additive findings**: (a) an unreconciled **ADR-003↔008 vector-index conflict** the ADRs themselves don't resolve; (b) an **ADR-006↔`PipelineRun` coordination gap** (independent reinvention of supersession); (c) one **unmeasured question** — cross-stage write overlap. The four "shifts" below are demoted accordingly; treat them as decisions/seams, not discoveries. An earlier draft also mis-anchored the model-provider point on `claimify` (an abandoned cost experiment, recorded in ADR-005: _"~$30 to process 5 documents… does not scale"_); §3 is recalibrated.

On what already works: `StageRepository` + adapter-over-protocol, content-addressed chunks, and `aizk_uuid` relating stages instead of coupling them are **genuinely task-oriented** (Calcado Pt II) — confirmed, not belabored.

## 1. The versioned-derived-state spine, and a real ADR-003↔008 conflict beneath it

**The actual unreconciled gap (additive finding a):** ADR-003 stores embeddings **inside SQLite** — _"a dedicated vec0 virtual table (from sqlite-vec) keyed by our primary entity IDs"_ (`CREATE VIRTUAL TABLE chunk_embedding USING vec0(...)`).
ADR-008 instead chooses a PyLate/Voyager index **on disk**, _"Python service only (no external DB); keep index on disk with periodic rebuilds,"_ using ColBERT **per-token multi-vectors** with MaxSim — and explicitly **rejects** single-vector dense retrieval.
These are two incompatible vector-index designs for the same data, and **neither ADR reconciles the other** — the most concrete thing the exercise surfaced, but **not action-forcing.**
ADRs here are point-in-time research, not binding contracts, and ADR drift is expected; the binding choice (vectors in SQLite vs. an on-disk index) lands in the **retrieval/embedding spec** when that read-side change is actually built, on the evidence then.
Note it as context, not a gate. (An earlier draft invented a tidy "CQRS split" framing and walked past this real conflict; that framing is retracted.)

**The spine that _is_ confirmed:** ADR-006 independently arrived at versioned-derived-state supersession — _"a splitter_version bump (re-chunking) or a canon_version bump (re-clustering) **supersedes** prior artifacts rather than overwriting them"_; _"canon_version plays the same role for entity clustering that splitter_version plays for chunking."_
That is `PipelineRun`'s active/superseded model at a corpus `scope_key`, reinvented without citing it (see the Reminder — additive finding b).

**Invalidation is three mechanisms, not one (grounded in ADR-005/006):**

- **Chunking self-invalidates by content-addressed `chunk_id` churn**, not a run pointer — _"any difference in chunk_ids between runs IS the invalidation signal."_
- **Persisted, model-dependent artifacts** (contextualized deltas, canonical clusters) need run/version stamps for reproducible, promotable rebuilds.
- **Embeddings are not "rows" to stamp.**
  The retrieval index is per-token multi-vector held by the index itself (ADR-008), and **mention** context embeddings are deliberately recomputed from `(chunk_id, span)`, _"so an encoder change strands no persisted vectors."_
  So the thing to make promotable is the **chunk / contextualized-chunk text + the index-build run**, not an "embedding row."

**Compaction is the back half:** ADR-006 — _"without the chunk_id-churn cleanup… superseded embeddings and mentions accumulate… plan for periodic compaction… otherwise storage grows with revision count."_
That is the deferred `artifact-compaction-retention` change; a versioned-derived-state model is incomplete without it.

**Caveat that bounds the whole section:** ADR-003 already sanctions **Meilisearch as a secondary index "fed from SQLite via an indexing job."**
If the read side is an off-the-shelf index, it owns its own IDs and lifecycle, and run-stamped _result→source provenance through our IDs_ buys little.
What survives regardless of read-side tech is the narrow ask: **reproducible, versioned derived data** so a rebuild is deterministic.
Don't design the index; keep the producer reproducible.

## 2. Cross-stage concurrency is a present-tense harness decision (but don't build a controller)

ADR-003 forbids write fan-out: one SQLite file, one serialized writer, Litestream sensitive to concurrent writes between syncs.
Conversion already runs two writers (API + worker).
As stages are added (chunking exists; contextualization/extraction/canonicalization are ADR-only), each harness is another writer.

**Correction to an earlier draft:** `BEGIN IMMEDIATE` + `busy_timeout=5000` makes contenders **wait**, not fail — so the consequence of many concurrent writers is **latency + Litestream checkpoint churn**, not `SQLITE_BUSY` (that only fires after 5 s of sustained contention).
ADR-006 confirms the real pressure point independently: extraction _"inverts the write profile… two orders of magnitude more rows, written in bursts during ingest and backfill,"_ and _"sustained write throughput from corpus-wide entity backfill is the first trigger likely to hit, well before multi-node deployment."_

**The present-tense decision (real):** the harness is the next build group, so the harness must choose now whether stage workers run concurrently or sequentially/pipelined.
**The likely-right answer is the cheap one** — stages pipelined per-document with ADR-006's per-stage batched writes — **not** a global write-admission controller.
A controller is cross-cutting machinery justified by an **unmeasured** race; estimate cross-stage write overlap before building it (additive finding c).
The per-stage `concurrency_limit` on `StageRepository` bounds in-flight execution, which is fine for I/O-bound LLM/NER work; only the _commits_ serialize, and short `BEGIN IMMEDIATE` blocks keep that cheap until backfill volume says otherwise.

## 3. No shared model-provider client/budget — and embedding may be external after all

ADR-004 chose "DIY, OpenAI defaults, no gateway"; there is no LLM/embedding client in the codebase (grep: only S3 and arxiv/karakeep clients).
**Correction:** an earlier draft claimed embedding is CPU-first/self-hosted — but **ADR-007 (embedding) is an undecided stub** whose only content is a price table of _external paid_ providers (OpenAI/Cohere/Voyage/Jina).
ADR-008's CPU-first stance is about the _ColBERT retrieval index_, not the embedding decision.
So embedding may well be an **external-quota** stage, which **broadens** this concern: contextualization (LLM), an optional LLM fallback in canonicalization (ADR-006 cites Graphiti/Zep precedent), and possibly embedding could all contend for external quotas.

When two such stages run, they hit the same rate limit with zero coordination — the "unbounded fan-out makes things slower" 429 failure, compounding §2.

**Seam to preserve (not a service to build):** when a second external-provider consumer lands, inject a **shared per-provider limiter** into each stage's client.
The existing arxiv fetch already uses a shared module-level limiter singleton — the pattern exists; apply it to the model provider.
This is explicitly **not** the LLM gateway ADR-004 rejected.
Until then: don't let each stage hard-wire its own client.

## 4. Evaluation infrastructure — mostly ADR-006 restated; one architectural note

The earlier draft over-elevated this.
ADR-006 **already** mandates the validation gate verbatim — gold set, span-level NER eval, coreference, co-occurrence edge precision, create-vs-assign benchmarking, threshold calibration, _"the project must pass a validation gate"_ before topology-split work, and the embedding signal _"measured with and without it on the gold set before it is relied on."_
That is a per-stage quality gate ADR-006 owns, not a new app-wide discovery.

The one genuinely architectural note: **eval infra should be shared, not per-stage.**
`aizk.metrics` is OCR-only today (alignment / rouge / kendalltau — no clustering metric); it is the natural seed for a shared gold-set + eval-harness component that extraction, canonicalization, and retrieval will each need.
Separately, runtime observability wants a consistent **`(run_id, aizk_uuid, stage, work_unit_ref)`** correlation spine (Rapidflare's three-level IDs) before five stages each instrument differently.

## Non-shift: polling stays; background repair is just another stage (a credit, not a gap)

Cross-stage triggering should stay **polling, not pub/sub** — at single-writer SQLite scale with no low-latency need, push choreography adds failure surface for no benefit.
Keep `pipeline_events` as audit/read-model, not a bus.

ADR-006 adds deferred propagation — _"dependent edge repair, embedding invalidation, cache invalidation… tracked by dirty sets and background repair."_
This is **pull-based background work**, and crucially the `StageRepository` protocol **already accommodates it**: a repair stage is just another `StageRepository` whose eligibility query targets a dirty-set table.
No protocol generalization is needed — which is a point in the design's favor.
`pipeline_events` + `chunk_id` churn + `canon_version` bumps are the **invalidation-signal source** that populates the dirty sets a repair worker polls — the signal source, not a push bus.

## Reminder: cross-reference `pipeline-stage-runtime`'s `design.md` and ADR-006 — but do NOT "unify" them yet

ADR-006 (status: **Proposed**) independently invented `canon_version`/`splitter_version` supersession without referencing the `PipelineRun(stage, scope_key, version_stamps_json)` primitive that already exists.
The right response is a **cross-reference** so the graph author sees the prior art and evaluates reuse — **not** welding ADR-006 into the shipped primitive before its consumer exists (the companion note retracted a structurally identical premature-freeze move, and the run primitive has _already_ been bitten once by a premature weld — see W1 there).

Critically, the two models are **not** the same shape:

- **`canon_version` / `splitter_version`** are clustering/version stamps → these **can** map to a `PipelineRun` (corpus `scope_key`, `version_stamps_json`).
  Chunking can stay purely content-addressed and skip the run record.
- **`entity_id` lineage** is a different, richer model — _"immutable… never reused; a split or merge retires the old ID and mints successor IDs… retired IDs remain permanently resolvable through lineage events."_
  That is an **append-only lineage DAG** the run primitive's active/superseded binary **cannot and must not absorb.**
  Telling the graph author to "consume the run primitive" for lineage would flatten a DAG into a version pointer — a real misdirection the earlier "unify" framing risked.

So: cross-reference the version axis; keep entity-ID lineage as its own append-only log.

## Pointed takeaways — producer-side mechanisms that MUST hold

The load-bearing contracts this note reduces to, stated as mechanisms (not named structures).
Tagged by where they live today: **[locked]** already a requirement in the pipeline-stage-runtime spec; **[in specs]** realized in the open chunk/mention delta specs; **[tighten]** present only as a design/test convention, should become an explicit requirement; **[carry]** belongs in a not-yet-open spec.

- **Versioned, reproducible derived state with atomic generation-swap.**
  A model/input-dependent artifact belongs to a generation recording its input fingerprint + producing versions; rows are immutable once written; which generation is live flips atomically (≤1 active, never a gap); a re-run with unchanged inputs+versions is a no-op.
  **[locked + in specs]**
- **Identity matches determinism.**
  Deterministic stages derive identity from content, so id-churn IS the invalidation signal; model-dependent stages use generation/run-scoped identity.
  Neither mechanism is forced on the other.
  **[in specs]**
- **Two append-only logs, each co-committed with the state it records:** lifecycle transitions (status + event, one atomic write) and entity lineage (split/merge mints immutable successor IDs; retired IDs stay resolvable).
  Lineage is a DAG, NOT generation-swap — keep them distinct mechanisms.
  **[supersession in specs; lineage = carry → canonicalization]**
- **Provenance on the derived row, not by join.**
  Each artifact denormalizes source identity (and, for graph artifacts, the producing mention IDs) so re-cuts/splits/repairs route soundly after intermediate rows are compacted or deleted.
  **[in specs]**
- **Single serialized writer, batched per logical unit.**
  Writes co-commit through one writer, batched per-document / per-chunk, not row-at-a-time; no write-fan-out assumption.
  Concurrency is for execution, not commit.
  **[design decisions; optional consolidation into a baseline spec]**
- **Model-provider access through an injectable seam**, not a client constructed inside each stage — so a shared per-provider limiter/budget can drop in later.
  **[tighten → make an explicit requirement in the contextualization + extraction specs]**
- **Empirical stages gated by offline evaluation before activation** (gold set + quality metrics).
  **[carry → canonicalization spec, which owns the gold set]**
- **Compaction is the companion to supersession:** superseded generations must be reclaimable under an explicit retention policy without breaking lineage, audit, or repair.
  **[carry → artifact-compaction-retention]**
- **Deferred reconciliation is pull-based:** invalidation (id-churn, version bumps) records into dirty sets a repair worker polls — repair is just another stage, not a push bus.
  **[protocol already supports]**

## Honest bottom line

Most of this note **confirms** ADR-003/005/006.
The durable, additive content is three items: the **ADR-003↔008 index conflict** (known drift, not a gate), the **ADR-006↔`PipelineRun` coordination gap** (cross-reference, don't unify — and don't conflate version supersession with entity lineage), and the **cross-stage write-overlap question** (decide the concurrency model; measure before building a controller). §3 stands and broadens (embedding may be external); §4 collapses into "ADR-006 already says it; make eval infra shared."

Watch the **accretion**: a versioned spine + compaction + an admission knob + a provider budget + an eval harness + a repair work-unit type is, in aggregate, the skeleton of the framework the companion note's discipline warns against.
Each is individually defensible as a _seam_ or a _decision_; none should be built as standalone machinery now.
**What explicitly NOT to do:** build read-side architecture, an LLM gateway, a write-admission controller, or pub/sub.
The leverage is in keeping the **producer** reproducible and versioned, and in resolving the two ADR gaps before the graph stage forks them.
