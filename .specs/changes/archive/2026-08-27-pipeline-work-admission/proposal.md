# Proposal: Pipeline Work Admission

## Intent

A converted source does not reach the graph today.
Nothing creates contextualization or extraction work-units except a human calling an enqueue primitive from a notebook or a test, so the graph stage has never run against the live database and the corpus is collected but not connected.

The baseline is not violated by this — it specifies that work-units exist in bulk and incremental forms and that both produce identical results, but it never specifies that anything _causes_ a work-unit to exist.
That is the gap this change closes.

The gap will widen.
Each graph stage added later inherits the same problem, and each one added after a corpus already exists also needs its existing documents pulled through.
This change makes reaching a stage a property the stage declares over upstream state, so the same mechanism serves steady-state flow and new-stage absorption without a bespoke migration per stage.

**Terminology.**
_Admission_ is creating a work-unit that does not yet exist, derived from upstream artifact state.
It is distinct from _discovery_ in `pipeline-stage-runtime`, which selects already-queued units to claim.
This change adds admission; it does not alter discovery.

## User Stories

### Story: automatic-graph-admission

As the corpus owner, I want a converted source to reach the graph stages without me running anything by hand, so that ingesting a resource is enough to connect it — collecting and connecting are one act, not two.

### Story: new-stage-absorbs-corpus

As the graph's owner, I want a newly added stage to pull in the documents already in the corpus by declaring what it consumes, so that extending the pipeline is one declaration rather than a one-off backfill script written and discarded per stage.

### Story: visible-stage-coverage

As an operator, I want to see how many sources are behind at each stage and which ones, so that a document silently stalled between stages is a number I can look at rather than a gap I discover months later.

### Story: bounded-inference-spend

As the system's owner, I want automatic admission to be bounded and switchable, so that turning on the flow cannot quietly run up external inference cost on a self-hosted budget.

### Story: uniform-work-intake

As an operator, I want the graph service to submit, observe, and manage work the same way the conversion service does, so that I learn one set of controls for the whole fleet instead of a different shape per stage.

## Scope

Capabilities in build-dependency order: the admission contract first, then the graph intake surface that exposes it, then the two stages that declare against it, then the operator surface that reads it.

**In scope:**

- **`pipeline-work-admission` capability (new).**

  - A stage MAY declare a **pending-work derivation**: given upstream artifact state, the set of work-unit keys that should exist at this stage but do not.
    Declaring one is optional and feature-detected — a stage that declares none is fully conformant and is simply never admitted into automatically.
  - An **admission pass** that evaluates a declared derivation and enqueues the result through that stage's existing enqueue primitive, so existing `idempotency_key` dedupe makes repeated passes safe with no new dedupe mechanism.
  - A stage MAY declare a **capacity limit** over its own actionable backlog.
    The limit is a property of enqueue, not of any one caller: a stage at capacity refuses new work whatever path asked for it — intake, an admission pass, or a bulk command.
    Automatic admission is additionally off unless explicitly enabled.
  - What counts as pending is part of each stage's declared derivation, not a blanket guarantee.
    Contextualization's derivation covers re-converted sources, because its work-unit identity is per conversion output.
    Extraction's covers never-extracted sources only: its work-unit identity is per source and never re-enqueues a terminal unit, and automating re-extraction after an upstream change is a deferral the extraction stage records explicitly — honoring it here keeps that decision where it was made.

- **`graph-work-intake` capability (new).**

  - Work-unit intake operations on the graph service for the contextualization and extraction stages, mirroring the conversion service's job submission.
  - Intake refuses at capacity with the same rejection shape the conversion API already returns — a 503 carrying `Retry-After` — so one convention covers the whole fleet.
  - The graph service's existing job read, retry, and cancel operations carry no baseline requirements today.
    This capability establishes the graph work-intake contract; it does not retroactively specify the surfaces already shipped.

- **`chunk-contextualization` capability (delta).**
  Declares its pending-work derivation over conversion outputs: a source whose latest `ConversionOutput` has no current work-unit is pending.

- **`entity-extraction` capability (delta).**
  Declares its pending-work derivation over active chunking runs, generalizing the eligibility query the bulk enqueue path already uses.
  Also declares a staleness derivation — a source whose extraction consumed since-superseded upstream state is identifiable as stale — and a re-admission action limited to stale terminal units, so an operator can re-extract individually or in bulk without any automatic re-spend.

- **`operator-console` capability (delta).**
  Pending-admission counts as a per-stage column on the existing dashboard, and the list of a stage's pending sources on that stage's existing monitor page.
  Stale counts and stale-unit marking for any stage declaring a staleness derivation, so stale units can be selected for the stage's declared actions.
  No new console section or route; re-admission rides the console's existing declared-action machinery.

**Out of scope:**

- **The one-time corpus backfill.**
  Populating the graph tables from the existing corpus uses the bulk enqueue primitives that already exist and is already within baseline scope.
  It is being done separately and is not gated on this change.
- **Push-based enqueue from the conversion stage.**
  Deliberately rejected — see Approach.
- **Automatic re-admission of stale work.**
  No staleness condition — upstream supersession, or a model, prompt, or extractor version change — triggers re-admission by itself.
  Stale work is surfaced to the operator and re-admitted only through explicit action, individually or in bulk; automatic admission covers work that was never done, and never becomes a corpus-wide re-spend trigger.
- **Any new orchestration technology.**
  No scheduler service, no pub/sub, no queue broker.
  Admission is a bounded query run on an interval inside a process that already exists.
- **Keyterm extraction.**
  Its change is paused pending corpus evidence; if it ships later it declares a derivation like any other stage, which is the point.
- **Backpressure from downstream stages.**
  A stage's capacity limit reads its own backlog, not the depth of the stages after it.
- **Migrating the backfill commands onto the intake operations.**
  Once graph intake exists the backfill commands could become thin clients of it, giving one code path for every enqueue.
  That is a worthwhile follow-on, but folding it in here would make this change depend on a command another change owns.
- **Retrofitting conversion onto the shared capacity contract.**
  Conversion already enforces its own queue-depth limit at submission.
  Converging it onto the generic contract is a later consolidation, not a prerequisite; this change matches conversion's rejection shape rather than rewriting it.
- **Authorization redesign for graph intake.**
  Intake adopts whatever principal handling the graph service's existing operations already apply.
  Introducing a new authorization model is out of scope.

## Approach

**Pull, not push.**
Conversion could call the graph's enqueue primitive in the transaction that writes the conversion output — immediate and transactionally exact.
Rejected for three reasons: it makes `aizk.conversion` depend on `aizk.graph`, a direction currently clean and declared downstream-only; a missed push leaves a permanently invisible document with no repair path; and every future stage needs its own trigger welded to its own upstream.
A derivation query is self-healing by construction — it asks about state, not events, so a unit that failed to be created is simply created on the next pass.
This also matches the project's stated default of durable polling or pull-based repair.

**The derivation is already the house pattern.**
Extraction's bulk enqueue path does not learn about documents by being told; it queries for sources with an active chunking run and enqueues the answer.
That query, generalized and made a declarable part of stage registration, is the whole mechanism.
Contextualization is the stage missing its equivalent, because its upstream is a conversion output rather than a pipeline run.

**Optional and feature-detected, for the contract floor.**
Stages key off structurally different upstreams — a conversion output, an active run of another stage, something a future stage has not defined yet.
Making a derivation mandatory would shape the contract around the two stages that exist now.
Declaring one is therefore optional and queryable, mirroring how the runtime already refuses to require a shared work-unit table.

**One query, two consumers.**
The same derivation that feeds admission feeds the coverage view — counted rather than enqueued.
This matters beyond code reuse: it makes the operator surface structurally incapable of disagreeing with what admission will actually do.

**The two services should mirror each other, and intake is the only gap.**
Neither service distributes work — both stages run the same stage runner and their workers claim from the database, so a worker drains normally with its service stopped.
Observation and management are symmetric already: both list their units and expose retry and cancel, and the console covers both.
The single asymmetry is that conversion can be asked to create a job and the graph service cannot be asked to create anything.
Closing it makes one operator interface cover the fleet.

**The capacity limit sits at the enqueue seam, not at the intake surface.**
Conversion's queue-depth refusal is universal because conversion constructs a job in exactly one place, with the check immediately in front of it.
The graph constructs work-units in two places, both unguarded.
Putting the limit where units are constructed rather than in front of one caller keeps every path — intake, admission, bulk commands — subject to it, and keeps the admission pass free of any dependency on the service running.

**Bounding.**
Contextualization is LLM-backed, so admission is spend, and the ceiling is the stage's declared capacity rather than a cap invented for the admission pass.
Admission enqueues until the stage is at capacity and stops, exactly as an intake caller is refused at capacity.
Automatic admission is additionally off unless enabled, so switching it on is a deliberate act.

**Host.**
The likely home is a loop in the existing worker process rather than a new service — but see Open Questions.

## Schema Impact

No change expected to the tracked `conversion-api-openapi` schema.
This change adds no conversion API operations and modifies no conversion request or response model.
Its persistence effect is confined to creating rows in existing work-unit tables through existing enqueue primitives; its configuration effect is new settings fields, which the tracked schema does not cover.

The graph service's API **does** gain operations, and no schema is tracked for it — `.sdd/schema-config.yaml` defines a generator for the conversion API only.
A change that adds API operations with no schema under version control gives `sdd-verify` nothing to cross-check.
Adding a `graph-api-openapi` entry to the schema config is therefore proposed as part of this change, so the new intake operations land with a captured before/after diff rather than none.

## Open Questions

- **Where does the admission pass run?**
  A loop inside the existing `aizk-graph worker` process, or a separate command run on a schedule.
  The worker is simpler and needs no new process supervision; a separate command makes admission independently pausable without stopping execution, which matters if admission is what you want to stop when spend spikes.
  Decided in `design.md`.
