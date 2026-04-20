# Design: pluggable-fetch-convert

## Context

The conversion pipeline (`aizk.conversion`) processes queued jobs by fetching source content and converting it to Markdown.
Today the orchestrator directly imports Docling conversion functions and contains `if`/`elif` ladders for source-type detection (arxiv / github / KaraKeep asset / inline text).
Configuration is a flat namespace of `docling_*` fields, and the GPU semaphore is a module-level global in the orchestrator.

This refactor introduces Ports & Adapters boundaries without changing any observable behavior.
The pipeline is the sole user's project — there are no external consumers — so breaking changes to the API schema and env vars are acceptable without deprecation periods.

Constraints:

- SQLite database (WAL mode, single-writer).
- Subprocess-isolated conversion (fork per job for crash containment).
  The parent process owns job lifecycle; the child process runs conversion only.
- Existing test pyramid (unit, integration, contract) must remain green throughout.

## Decisions

### Decision: Generalize Bookmark to Source

**Chosen:** Rename the `bookmarks` table to `sources`.
Make `karakeep_id` nullable.
Add `source_ref` (JSON) and `source_ref_hash` (unique index).
Keep `aizk_uuid` as the stable relational and user-facing identity.
Retain `source_type` as derived metadata.
Jobs continue to FK via `aizk_uuid`.

**Rationale:** Every job needs a stable identity row for API listing, output history, and provenance.
Today that row is a `Bookmark`, which assumes KaraKeep.
Generalizing the same table avoids a new FK migration (`aizk_uuid` is already the FK on `ConversionJob`) and keeps the API surface (`aizk_uuid` filtering, output listing) working for all source types.
The only truly KaraKeep-specific column is `karakeep_id`; `url`, `normalized_url`, `title`, and `content_type` are useful for all sources.

**Source invariants:**

- Source is the canonical durable identity for anything the system can convert.
- Every job references exactly one Source (via `aizk_uuid` FK).
- `aizk_uuid` remains the stable user-facing identifier for listing outputs and correlating history.
- `karakeep_id` is nullable and only populated for KaraKeep-backed sources.
- `source_ref` is the canonical fetch instruction, persisted on the Source row.
- `url`, `normalized_url`, `title`, `content_type`, and `source_type` are mutable, derived metadata that may be enriched after ingest or fetch.
- Source dedup and job idempotency are separate concerns.
  A single Source can have many jobs.

**Known schema-vs-invariant gap (deferred to follow-up):** `source_ref` and `source_ref_hash` are modelled as `nullable=True` at cutover because the cutover migration only populates them for rows with non-null `karakeep_id`; any legacy row without a `karakeep_id` would otherwise fail the backfill.
Every row written by the post-cutover API has both columns populated, and no code path emits a Source without them.
A follow-up change SHALL add an Alembic revision that asserts non-null coverage and alters both columns to `NOT NULL`, closing this gap before any Postgres migration or widening of `IngressPolicy`.
Until then, `Source.source_ref` and `Source.source_ref_hash` MUST be treated as non-null in application code even though the schema permits null.

**Dedup model:** `source_ref_hash` is computed from each variant's `to_dedup_payload()` method — a canonical, normalized dict containing only the fields that define semantic identity for that variant.
The hash encodes the payload via `json.dumps(payload, sort_keys=True, separators=(",", ":"))` before SHA-256, so field declaration order, optional-default appearance, and future additive fields do not cause hash churn.
Two submissions with structurally identical `source_ref` share one Source row.
`normalized_url` is derived metadata for human discovery and cross-source-type search, not dedup.
This means the same content arriving via two different ingress paths (KaraKeep bookmark vs direct URL) creates two Source rows — the provenance is genuinely different.

Example payloads (each variant defines its own):

- `KarakeepBookmarkRef.to_dedup_payload() -> {"kind": "karakeep_bookmark", "bookmark_id": "<id>"}`
- `ArxivRef.to_dedup_payload() -> {"kind": "arxiv", "arxiv_id": "<normalized_id>"}`
- `UrlRef.to_dedup_payload() -> {"kind": "url", "url": "<normalized_url>"}`
- `InlineHtmlRef.to_dedup_payload() -> {"kind": "inline_html", "content_hash": "<sha256_of_bytes>"}` (not the bytes themselves, so the dedup payload stays small and semantically meaningful)

Rejecting `hash(SourceRef.model_dump_json())` directly: `model_dump_json` is fragile across field reorderings, default-value appearances, and variant evolution.
Per-variant canonical payloads are explicit about what "same source" means, and new optional fields can be added without changing the hash of existing rows.

**`source_type` vs `source_ref.kind`:** These are distinct concepts. `source_ref.kind` is the ingress/ref shape (e.g., `karakeep_bookmark`). `source_type` is the resolved semantic origin (e.g., `arxiv`, `github`, `other`) — a KaraKeep bookmark can resolve to arxiv semantics after inspection. `source_type` is retained as a cached, derived classification used for UI/filtering, populated during the fetch phase.

**Alternatives considered:**

- **Separate Source + KarakeepBookmark attachment tables:** cleaner separation but adds a join and migration for one nullable column.
  No second attachment type exists yet.
  Premature abstraction; evolve if attachment-specific columns grow.
- **Make bookmark FK nullable, no rename:** cheapest change but leaves non-KaraKeep jobs without a stable identity.
  Job listing hard-fails if bookmark is null.
  Rejected because it doesn't fix the identity gap.
- **New `source_id` FK column on jobs:** not needed.
  `aizk_uuid` is already the FK.
  Adding `source_id` would require migrating every job row for no immediate benefit.

### Decision: API materializes Source identity; worker enriches Source metadata

**Chosen:** Single-owner split between the API and the worker.

- **API (identity owner):** validates the incoming `source_ref`, canonicalizes it, computes `source_ref_hash`, creates-or-reuses the Source row, computes the idempotency key, and persists the job with `aizk_uuid` and `source_ref` denormalized on the job record.
  All immutable identity data is fixed at submit time.
- **Worker (enrichment owner):** resolves/fetches content via the fetcher chain, enriches mutable metadata on the existing Source row (`url`, `normalized_url`, `title`, `source_type`, `content_type`), runs conversion, and emits outputs.
  The worker never creates or deletes Source rows.

**Rationale:** Today the API is already the writer of bookmark rows and idempotency keys (see `jobs.py:159-186`).
Splitting creation across the API and worker would reopen the uniqueness-conflict window (two workers or an API+worker racing to insert the same `source_ref_hash`), leave queued jobs without a valid Source row until the worker picks them up, and make `aizk_uuid` mutable from the caller's perspective.
Keeping identity creation on the API side preserves today's semantics: a successful submission always implies a durable, unique, queryable Source row.

**Invariants:**

- A queued job always has a Source row with a stable `aizk_uuid`.
- `source_ref`, `source_ref_hash`, `aizk_uuid`, and `karakeep_id` (when applicable) are immutable after submit.
- The worker only writes to mutable-metadata columns on the Source row.
- Idempotency-key computation is an API-side concern and shares the same inputs the worker uses (see manifest `config_snapshot`).

**Concurrent-submit dedup semantics:** Two concurrent API submissions carrying the same `source_ref_hash` SHALL both succeed and reference the same Source row (same `aizk_uuid`).
The API uses `INSERT ... ON CONFLICT (source_ref_hash) DO NOTHING` followed by a `SELECT` on `source_ref_hash` to obtain the canonical row (whether the current transaction inserted it or lost the race).
A race SHALL NOT produce two Source rows, an IntegrityError bubbled to the caller, or an aborted transaction.
Each submit produces its own Job row; dedup between jobs is a separate concern handled by `idempotency_key`, not by `source_ref_hash`.

**Enrichment conflict resolution:** Writes to mutable-metadata columns (`url`, `normalized_url`, `title`, `source_type`, `content_type`) SHALL follow last-writer-wins semantics.
Two jobs on the same Source row that enrich concurrently are not serialized by the spec; whichever write commits last is observed on the Source row.
The Source row's mutable metadata is an advisory cache for UI listing and search.
The authoritative per-job values (specifically `content_type` as seen by the converter) live on the job's manifest, not on the Source row.
A later job's enrichment overwriting an earlier job's value is not a correctness bug — it is the defined behavior.

**Alternatives considered:**

- **Worker creates the Source row on first fetch:** matches the wording of the legacy `Register bookmarks with stable internal identifiers` worker requirement, but inconsistent with current code (API creates the row) and introduces a race between submission and worker pickup.
  Rejected.
- **Shared write path (both layers can insert):** forces cross-layer uniqueness handling (upsert semantics, `ON CONFLICT DO NOTHING`) everywhere Source is written.
  Adds complexity for no caller-visible benefit.
  Rejected.

### Decision: Ports & Adapters with dependency injection

**Chosen:** Hexagonal Architecture.
Protocols (ports) in `aizk.conversion.core`; concrete adapters in `aizk.conversion.adapters`; role-specific builders in `aizk.conversion.wiring` assemble the graph per process role.
The orchestrator receives fetcher and converter resolvers via constructor injection.

**Rationale:** The orchestrator's job is to coordinate fetch -> convert -> upload.
It should not know which converter or fetcher is in use.
DI via constructor-injected callables makes the dependency explicit in the signature, avoids global state, and makes tests trivially injectable.

**Alternatives considered:**

- **Service locator (global registry lookup inside orchestrator):** simpler initially, but the orchestrator acquires a hidden dependency on the global registry.
  Tests must mutate shared state.
  Rejected because DI costs one parameter and eliminates the problem.
- **Framework DI (e.g., dependency-injector library):** adds a framework for a graph that has ~10 nodes.
  Over-engineered for this project's scale.

### Decision: Role-specific wiring builders

**Chosen:** The `aizk.conversion.wiring` package exposes `build_worker_runtime(cfg)`, `build_api_runtime(cfg)`, and `build_test_runtime(cfg)`.
Each builder registers only the adapters, probes, and accepted source kinds appropriate for that process role.

**Rationale:** Worker, API, and test processes have different runtime needs.
The worker needs all fetcher/converter adapters and the GPU guard, and returns a `DeploymentCapabilities` descriptor over `FetcherRegistry.registered_kinds()`.
The API needs a kind-validation gate and schema awareness but not converter adapters, and returns a `SubmissionCapabilities` descriptor over `IngressPolicy.accepted_submission_kinds`.
Tests need fake resolvers.
Worker and API do NOT produce identical accepted kind sets — they answer different questions (see "Separate internal dispatch from public ingress; gate via IngressPolicy").
One monolithic `build_orchestrator()` either over-provisions (API gets converter adapters it doesn't use) or under-specifies (which probes run where).
Role-specific builders make startup probes, registered dispatch kinds, and publicly accepted submission kinds explicit per process type.

**Alternatives considered:**

- **Single `build_orchestrator(cfg)` with conditional branches:** works at small scale but accumulates `if role == ...` branches.
  Rejected because role-specific builders are just as simple and more explicit.

### Decision: Two fetcher roles — ContentFetcher and RefResolver

**Chosen:** Two distinct protocols.

- `ContentFetcher.fetch(ref) -> ConversionInput` (terminal — returns bytes).
- `RefResolver.resolve(ref) -> SourceRef` (intermediate — refines a ref into a more specific one).

The orchestrator recurses on resolved refs with a configurable depth cap (default 2).
Today's longest real chain is 1 hop (KaraKeep bookmark → terminal fetcher); a cap of 2 permits exactly one unexpected extra hop before failing, so genuine mis-wiring surfaces as an error rather than silently delegating through more levels.
The cap is configurable precisely because a future ingress path may legitimately need more hops — but raising it should be a deliberate configuration change, not a silent default.

**Role is declared at registration, determined structurally at dispatch:** The registry exposes two distinct registration entry points — `register_content_fetcher(kind, impl)` and `register_resolver(kind, impl)` — which document caller intent and keep the call-site readable.
Dispatch-time role is determined by `isinstance(impl, RefResolver)` against the `@runtime_checkable` protocol: an adapter without a `resolves_to` ClassVar and `resolve` method cannot satisfy `RefResolver`, so the check is reliable for the protocols as specified.
Kind uniqueness is enforced in the registry: the same `kind` cannot have both a content fetcher and a resolver registered, regardless of which entry point was used.

**Rationale:** Encoding the role at the protocol level (via the presence of `resolve` + `resolves_to` on RefResolver) means a single class cannot accidentally satisfy both — ContentFetcher is minimal (one method + `produces`) and RefResolver adds `resolves_to` + `resolve`.
Keeping role-determination structural at dispatch time means the orchestrator does not carry a separate role tag on each dispatch, and the registry stores only `impl` per kind — one fewer representation to drift.
A `ContentFetcher` that accidentally returns a `SourceRef` is a static type error; each protocol has a single, concrete return type.

The depth cap prevents infinite resolver chains.
In practice the longest chain is 1 hop (KaraKeep bookmark -> arxiv/github/url ref); the default cap of 2 is one hop above that ceiling.
When `FetcherDepthExceeded` fires, the error message SHALL name the current cap, the chain of kinds traversed, and the configuration key to raise the cap — so the first response when someone legitimately needs a longer chain is "bump the config," not "read the source."

**Chain completeness is a wiring-time responsibility, not a declared static property:** A resolver chain is only useful if every kind the chain can reach is itself registered with a handler.
Rather than encode each resolver's possible emissions as a protocol attribute and verify the graph statically, the composition root is responsible for registering the full set of reachable kinds.
If a resolver returns a ref whose kind is not registered, the orchestrator raises `FetcherNotRegistered` at runtime for that job.
In practice there is one resolver (KaraKeep), its emission set is fixed, and anyone deploying the service registers the terminal fetchers alongside it — so the runtime-catch window is narrow.
The cost of stronger static verification (a declared emissions attribute on every resolver) is not justified by the class of bugs it prevents at this scale.

**Alternatives considered:**

- **Single fetcher protocol returning `ConversionInput | SourceRef`:** union return type is too permissive; any fetcher can return either thing without static error.
  Isinstance on every return value is repetitive.
- **Fetcher delegation inside the adapter (resolver calls another fetcher internally):** hides delegation, risks unbounded recursion, requires injecting the resolver callable into adapters (more wiring, harder to test).
- **Store the role as a separate tag alongside the impl in the registry:** previous design.
  Rejected because the protocols themselves distinguish the two roles structurally (`RefResolver` has `resolve` + `resolves_to`; `ContentFetcher` has `fetch` + `produces`), and every adapter declares the structure explicitly via ClassVars.
  Keeping a separate role tag was redundant and drifted from the adapter-declared source of truth; dropped in favor of `isinstance(impl, RefResolver)` at dispatch.
- **Declare each resolver's possible emissions as a protocol attribute so the wiring layer can statically verify chain termination:** declarations exist (see `RefResolver.resolves_to`) and are consumed at wiring time by `validate_chain_closure`; runtime `FetcherNotRegistered` remains the dispatch-time guard for any ref returned outside the declared set.

### Decision: Capability-based converter registry

**Chosen:** Each `Converter` declares `supported_formats: frozenset[ContentType]` (a class-level attribute).
One registry, indexed by `(content_type, impl_name)`.
Lookup: `registry.resolve(ContentType.PDF, "docling") -> DoclingConverter`.

**Rationale:** With 7+ content types (pdf, html, image, docx, pptx, xlsx, csv), maintaining per-format registries or per-format protocols would be bookkeeping for no gain.
A single registry with capability indexing handles a Docling that supports {pdf, html} and a Marker that supports {pdf} equally well.
Mix-and-match (Docling for HTML, Marker for PDF) is natural because lookup is per-format.

**Alternatives considered:**

- **One converter handles all formats (wholesale swap):** maps to how Docling is built, but plugging in a PDF-only tool (Marker) requires the replacement to implement a no-op HTML path or mix-and-match outside the protocol.
  Rejected.
- **Per-format protocol (PdfConverter, HtmlConverter, ...):** clean at 2 formats; at 7+ it creates parallel hierarchies.
  Rejected for scaling reasons.

### Decision: SourceRef as a pydantic discriminated union

**Chosen:** `SourceRef = Annotated[KarakeepBookmarkRef | ArxivRef | GithubReadmeRef | UrlRef | SingleFileRef | InlineHtmlRef, Field(discriminator="kind")]`.
Each variant is a frozen pydantic model with a `kind: Literal[...]` field.
Stored as JSON on the Source row (canonical) and denormalized on the job record.

**Rationale:** Pydantic handles serialization, deserialization, and discriminator-based dispatch.
Adding a new source = adding a variant + a fetcher adapter; no schema migration required (JSON column accepts any valid variant).
Frozen models enforce that refs are immutable after creation.

**Alternatives considered:**

- **String-keyed dict (opaque JSON):** no type safety on read; callers must manually validate and dispatch.
  Rejected.
- **Separate table per source type with FK from job:** relational purity but schema migrations for every new source.
  Rejected.

### Decision: InlineHtmlRef carries a size-capped payload

**Chosen:** `InlineHtmlRef` embeds the inline HTML/text content directly in the ref, with a hard size cap of 64 KiB measured on the raw body bytes (not the serialized JSON form).
Typical HTML-shaped content expands by ~1.3× when JSON-escaped, so worst-case persisted column value is ~85 KiB; SQLite handles this comfortably.
The `InlineContentFetcher` returns the embedded bytes as a `ConversionInput`.
This is a documented exception to the general principle that `SourceRef` variants are lightweight pointers.

**Rationale:** Text bookmarks in KaraKeep are typically \<5KB (plain text wrapped in `<html><body><pre>`).
A 64KB cap bounds the JSON column bloat while covering all realistic inline-text cases.
The alternative (staging inline content to a temp blob or database row) adds a storage step, a cleanup lifecycle, and a failure mode — all for a rare case with small payloads.
The exception is bounded, documented, and enforced by pydantic validation on the model.

The `KarakeepBookmarkResolver` remains purely a ref resolver: it inspects the bookmark and returns either a refined ref (ArxivRef, GithubReadmeRef, UrlRef) or an `InlineHtmlRef` with the text content embedded.

**Why the "refs are pointers" principle doesn't apply here:** The principle exists to prevent multi-MB payloads bloating the job table.
InlineHtmlRef is capped at 64KB and applies only to a single low-volume case (text bookmarks).
If a future ref variant would carry substantial payloads, the pointer principle reasserts — use staged storage instead.

**Alternatives considered:**

- **Staged blob row (pointer-style):** clean separation but adds a temp-content table, insert/cleanup lifecycle, and a new failure mode for a case that produces \<5KB payloads.
  Over-engineered.
- **Allow KaraKeep resolver to return `ConversionInput` directly:** breaks the pure RefResolver contract.
  Rejected.

### Decision: GPU admission control stays in the parent process

**Chosen:** The GPU concurrency gate remains a parent-process concern.
The current `threading.Semaphore` acquired before `_spawn_conversion_subprocess()` is the correct mechanism.
The refactor wraps it as a `ResourceGuard` protocol injected into the parent-side orchestration layer (the supervision/loop code), not into converter adapters.

**Rationale:** Conversion runs in forked subprocesses for crash isolation.
A `threading.Semaphore` in the parent's thread pool gates how many GPU-consuming subprocesses can run concurrently — this works because the parent thread holds the semaphore while the child runs.
If the guard were inside the converter adapter (which runs in the child), each forked process would have its own semaphore copy and the global cap would disappear.
This is a direct path to GPU OOM.

The guard remains a parent-process `threading.Semaphore` because:

- The parent dispatches jobs via `ThreadPoolExecutor`; each thread acquires the semaphore before spawning a subprocess.
- The semaphore is shared across threads in the parent (standard threading primitive).
- The child process never sees or needs the semaphore — it just runs conversion.

**Guard lifetime and release ownership:** The worker thread that acquires the guard is the sole releaser.
The guard is held via `with guard:` wrapping the full subprocess lifecycle — spawn, supervise, and reap (i.e., `process.join()` has returned or the child has been force-killed).
Release happens when the `with` block unwinds in the acquiring thread, whether conversion succeeded, the child crashed, the supervision loop raised, or the parent cancelled.
The supervision loop's role is to detect child termination and return control to the worker thread (signalling success, crash, or timeout); it does not call release directly.
This keeps the protocol minimal — a context manager is all that is needed — and avoids cross-thread release semantics.

If the worker thread itself dies (e.g., uncaught exception before the `with` unwinds), the semaphore leaks.
This is an accepted risk: the parent process is designed to crash loudly under such conditions (supervisor restarts it), and recovering from a half-released guard is more complex than restarting.

**Scope of admission control — GPU-specific:** The guard is semantically a GPU admission gate.
Operators provision it to match GPU capacity (one slot per physical GPU by default); config and env vars are GPU-scoped.
Each `Converter` declares a class-level `requires_gpu: bool` attribute.
The orchestrator enters `with guard:` only when the dispatched converter has `requires_gpu == True`; a converter with `requires_gpu == False` spawns without contending on the GPU guard.

Today the only registered converter is `DoclingConverter` with `requires_gpu = True`, so in the current deployment every conversion subprocess acquires the guard.
The `requires_gpu == False` branch is defined by the protocol but unexercised until a non-GPU converter lands (e.g., a future Pandoc or text-only adapter).
Declaring the flag now — rather than deferring it to the change that adds the first non-GPU converter — fixes the semantics of the guard as GPU-scoped and keeps the bypass mechanism a one-line declaration on the future converter rather than a protocol change.

Adapter-level guards (inside the child process) are available for optional intra-process subphases (e.g., if a converter has multiple GPU-bound steps it wants to serialize internally), but they do not replace the parent-level gate.

**Alternatives considered:**

- **Semaphore inside converter adapters (original design):** each forked child gets its own copy.
  Global GPU cap vanishes.
  Rejected — this was the critical flaw identified in review.
- **`multiprocessing.Semaphore` or OS-backed file lock:** would work across processes but adds complexity (shared-memory cleanup, file lock lifecycle).
  Not needed because the parent-process threading gate already solves the problem.
- **Module-level semaphore (current state):** works but is a global, making it untestable and invisible in the dependency graph.
  The refactor wraps it as an injected `ResourceGuard` at the parent/supervision level.

### Decision: source_ref persistence — typed on read

**Chosen:** `source_ref` JSON column is validated via the pydantic discriminated union on every read (i.e., the ORM/repository layer deserializes to `SourceRef` automatically).

**Rationale:** Catching corruption or version-skew at read time is strictly safer than deferring to the point of use.
The deserialization cost is negligible (single-row reads, not bulk scans).
The column is already JSON with a discriminator — pydantic does the work either way.

**Alternatives considered:**

- **Opaque JSON validated only at ingress/egress:** defers errors to the orchestrator, which then needs defensive parsing.
  No benefit given the column is always read one job at a time.

### Decision: Separate internal dispatch from public ingress; gate via IngressPolicy

**Chosen:** Three concepts that earlier drafts collapsed into one are now separated:

1. **Internal dispatch kinds** — the full `SourceRef` superset the worker can dispatch via the fetcher chain.
   Persistence (`Source.source_ref`), manifests (`submitted_ref` / `terminal_ref`), and worker dispatch all operate on this wide union.
2. **Publicly submittable ingress kinds** — the narrower subset the deployment accepts at its public API surface.
   This is a deployment policy, not an adapter property.
3. **Not-yet-wired future kinds** — skeleton classes (e.g., `SingleFileFetcher`) that exist in the codebase but are not registered in the composition root.
   They are invisible to both dispatch and ingress.

The wiring layer produces two distinct descriptors:

```text
DeploymentCapabilities (worker-side):
  registered_kinds: frozenset[str]                    # every kind the worker can dispatch
  content_types_for(kind) -> frozenset[ContentType]   # terminal content types this kind can produce
  converter_available(content_type) -> bool           # a converter is registered for this content_type
  startup_probes: list[Probe]                         # adapter-declared probes

SubmissionCapabilities (API-side):
  accepted_submission_kinds: frozenset[str]           # source_ref.kind values publicly accepted
```

`DeploymentCapabilities.registered_kinds` is sourced directly from `FetcherRegistry.registered_kinds()`.
`SubmissionCapabilities.accepted_submission_kinds` is sourced from an `IngressPolicy` configuration value (e.g., from config file, env var, or deployment manifest), NOT from registry membership and NOT from any adapter class attribute.

**Invariant:** `accepted_submission_kinds ⊆ registered_kinds`.
Wiring validates this at startup and raises a typed configuration error if the policy references a kind that is not registered.
The reverse subset is explicitly NOT required: the worker may dispatch kinds (e.g., `"arxiv"`, `"inline_html"` as resolver targets from KaraKeep) that the API does not accept as top-level submissions.

**Rationale:** Public-ingress acceptability is a deployment concern (what does this particular deployment expose to its callers?), not an intrinsic property of the adapter implementation.
The same `UrlFetcher` class might be publicly submittable in a deployment that intends direct-URL ingress and not in a deployment that only exposes KaraKeep ingress — without any change to the adapter code.
Encoding the policy on the adapter class forces the adapter module to know about deployment intent; lifting it to `IngressPolicy` in wiring keeps adapters context-free and makes the policy a single configurable value per deployment.

Separating `DeploymentCapabilities` from `SubmissionCapabilities` also removes the previous contradiction where worker-dispatch and public-ingress were claimed to share a single `accepted_kinds` set.
They are intentionally different sets, derived from different sources, consumed by different layers.

**Rollout:** At cutover (PR 6b), `IngressPolicy.accepted_submission_kinds = {"karakeep_bookmark"}`.
Worker wiring still registers the full set (`karakeep_bookmark` resolver plus `arxiv`, `github_readme`, `url`, `inline_html` content fetchers) so `validate_chain_closure` passes.
Widening public ingress later (e.g., to accept `UrlRef` directly) is a configuration change to `IngressPolicy`, not an adapter change or a rebuild.

**Chain-closure validation operates on `registered_kinds`, not on the submission subset.**
The wiring-time check (see "Validate resolver chain closure at wiring time" in the delta spec) walks the full registered DAG; the submission subset never participates.

**Alternatives considered:**

- **`api_submittable: ClassVar[bool]` on each adapter class (previous design):** encodes deployment policy as an adapter property.
  Rejected because the same adapter may be submittable in one deployment and not another — policy belongs in wiring config, not the adapter module.
  This also forces every new adapter to declare a policy flag that may or may not reflect the operator's actual intent.
- **Gate only on `FetcherRegistry.registered_kinds()` and require that set to equal the public ingress set:** collapses the three concepts into one.
  Forces worker dispatch and public ingress to share identity, which is exactly the constraint the resolver chain (KaraKeep → arxiv/url/github_readme/inline_html) violates.
  Rejected.
- **Keep an `is_ready(kind)` concept and a separate declared-accepted-kinds set on `DeploymentCapabilities`:** requires the composition root to maintain two interleaved lists.
  Rejected — the three-concept separation above is cleaner.
- **Merge PR 6a, PR 6b, and PR 7 into one atomic deploy:** simpler but loses the incremental rollout advantage and makes the PR larger.
  Acceptable for a sole user but less reviewable.
- **Let invalid jobs fail at worker fetch time:** technically works (the job gets FAILED_PERM) but wastes a queue slot and is confusing.
  Rejected.

### Decision: Narrow public JobSubmission contract via IngressSourceRef

**Chosen:** The public `POST /v1/jobs` request schema accepts an `IngressSourceRef` — a discriminated union restricted to the variants in `IngressPolicy.accepted_submission_kinds`.
At cutover, `IngressSourceRef = Annotated[KarakeepBookmarkRef, Field(discriminator="kind")]`.
The internal `SourceRef` discriminated union (all six variants) remains the shape persisted on the Source row, denormalized on the job record, emitted in manifests, and dispatched by the worker.

**Rationale:** The public OpenAPI schema and the internal dispatch union answer different questions.
Advertising every `SourceRef` variant in `JobSubmission` while runtime policy returns 422 for most of them produces a weak contract: the schema says "this shape is valid" while runtime says "not in this deployment."
A narrow request schema says "this deployment accepts exactly these shapes" — the schema and the runtime gate agree.
Widening is a deliberate public-contract change (a new `IngressSourceRef` variant union) coordinated with widening `IngressPolicy`.

`JobResponse.source_ref` retains the wide `SourceRef` union: callers reading job state may see any kind the resolver chain produced (e.g., a job whose `terminal_ref.kind == "arxiv"`), and the response surface should not fail schema validation on a narrower subset than what the system actually stores.

**Alternatives considered:**

- **Single wide `SourceRef` union on both request and response, 422 runtime gate:** produces the schema/runtime mismatch described above.
  Rejected in favor of schema-runtime alignment on the request side.
- **Wide union on request with a separate flag indicating support per variant:** forces callers to parse both the union and a capability flag.
  Rejected as duplicative of the 422 approach.

### Decision: Manifest evolution — version bump and flat submitted/terminal ref blocks

**Chosen:** Bump `ConversionManifest.version` from `"1.0"` to `"2.0"`.

- The manifest carries two typed ref blocks, both required:
  - `submitted_ref` — the `SourceRef` the caller supplied at submit time (the job's ingress shape).
  - `terminal_ref` — the ref whose `ContentFetcher` actually produced the converted bytes (the **terminal fetch state**).
    This answers "what document was this?"
    for the converted output.
- Both blocks use the same `SourceRef` discriminated union (keyed on `kind`).
  Examples of `terminal_ref`:
  - `{"kind": "url", "url": "..."}` for jobs that terminated at a direct URL fetch.
  - `{"kind": "arxiv", "arxiv_id": "..."}` / `{"kind": "github_readme", "owner": "...", "repo": "..."}` for jobs resolved from a KaraKeep bookmark (or submitted directly) that terminated at these fetchers.
  - `{"kind": "inline_html", "content_hash": "..."}` for inline-text jobs.
  - `{"kind": "karakeep_bookmark", "bookmark_id": "..."}` when the KaraKeep bookmark was itself the terminal fetch (i.e., content bytes came from KaraKeep itself — today this happens for PDF-asset and precrawled-archive bookmarks where KaraKeep is the byte source).
- For direct submissions (no resolver hop) `submitted_ref == terminal_ref` structurally; for a KaraKeep bookmark that resolved to arxiv, `submitted_ref.kind == "karakeep_bookmark"` and `terminal_ref.kind == "arxiv"`.
  Readers always see the same shape — two blocks, always present — and may do an equality check if they care about "was this a direct submission?"
- Chose flat/always-present over optional-ingress-when-different: consumers of the manifest (analytics, debugging, UI) always want to know both ends, and a uniform shape eliminates the reader-side branch.
  The marginal byte cost of duplication for direct submissions is negligible.
- `ManifestSource.url`, `normalized_url`, `title`, `source_type`, and `fetched_at` become `str | None`.
  Populated from the worker's enrichment pass when present; null for sources that have no meaningful URL/title (e.g., inline text).
- `config_snapshot` adds a required `converter_name: str` field alongside the adapter-supplied output-affecting fields.
  The Docling adapter contributes today's fields unchanged, preserving the structural contents of the snapshot for Docling deployments.
- Readers: the storage/manifest layer accepts both `"1.0"` and `"2.0"`.
  Version-specific reader classes (e.g., `ConversionManifestV1`, `ConversionManifestV2`) dispatched by the `version` field keep `extra="forbid"` on both. v1 is read-only (no new writes). v1 manifests continue to be readable from S3 for existing outputs; no migration rewrites v1 manifests in place.

**Rationale:** The baseline `ConversionManifest` at `storage/manifest.py:84-95` requires non-null `url`, `normalized_url`, `title`, and `karakeep_id`.
A direct `UrlRef` has a URL but no `karakeep_id`; an `InlineHtmlRef` has neither.
Under today's contract, a non-KaraKeep job literally cannot serialize a valid manifest.
That contradicts the proposal's claim that output endpoints are unchanged and non-KaraKeep sources are first-class.
A version bump makes the break explicit, the typed `submitted_ref` and `terminal_ref` blocks keep provenance semantically meaningful across all source types, and nullable URL/title fields remove the KaraKeep assumption from the output contract.

**Alternatives considered:**

- **Keep manifest version 1.0, make `karakeep_id` Optional:** silent contract change, consumers of existing manifests (or future downstream readers) cannot tell v1-with-new-semantics from v1-classic.
  Rejected — version bumps exist precisely for this.
- **Per-kind provenance as flat optional fields at manifest root (`arxiv_id`, `github_owner`, etc.):** clutters the top level and requires readers to know which fields correlate with which kind.
  Typed nested blocks are cleaner.
- **Single `provenance` block with optional `ingress` block only when different:** denser and slightly smaller on disk, but readers must branch on presence/absence — a cost paid by every consumer forever for a tiny byte saving.
  Rejected in favor of always-present `submitted_ref` + `terminal_ref` for uniform reader experience.
- **Drop provenance from the manifest entirely and rely on `source_ref`:** `source_ref` is the fetch instruction, not a record of what was actually fetched.
  Provenance in the manifest answers "what was this document?"
  — a different question.

### Decision: Env-var namespace — hard break

**Chosen:** Rename `AIZK_DOCLING_*` to `AIZK_CONVERTER__DOCLING__*` (and fetcher equivalents) in a single PR.
No compatibility shim.

**Rationale:** Sole user; no external consumers.
A deprecation window adds code (read-both, warn) that would be deleted one release later.
Hard break is simpler and the deployment env update is a single config file change.

### Decision: Idempotency key includes converter name; computed API-side at submit

**Chosen:** The idempotency key hash incorporates: `source_ref_hash` + `converter_name` + converter-scoped config snapshot hash.
The key is computed at the API submit path — the API is the writer of the `idempotency_key` column and the dedup gate runs before a job is ever queued.

**Key stability — intentionally broken, historical keys recomputed in-place:** Post-refactor keys do not match pre-refactor keys for the same content.
Two inputs change: `source_ref_hash` replaces the old `aizk_uuid`-based key component, and `converter_name` is newly incorporated.
As part of the same Alembic migration that renames `bookmarks` → `sources` and backfills `source_ref` / `source_ref_hash`, existing `idempotency_key` column values SHALL be recomputed using the new formula: for each historical job row, the migration reads its backfilled `source_ref_hash` from the referenced Source row, assumes `converter_name = "docling"` (the only converter in use pre-refactor), and substitutes today's Docling-specific config snapshot as the converter-supplied snapshot input.
After the migration, every row — historical and new — carries a key under the unified formula.
Recomputation means a resubmission of pre-refactor content post-refactor correctly matches the stored key and is recognized as an idempotent replay (rather than silently creating a duplicate job).

**Snapshot structural equivalence (separate from hash equivalence):** The Docling adapter's `config_snapshot()` SHALL contribute the same field set as today's Docling-specific config hash — including `picture_description_enabled` and any other output-affecting values the pre-refactor formula included.
This is a structural contract on the snapshot dict (same semantic field set with the same values for the same input config), not a claim that the final hash matches.
Field-key renames inside the snapshot — e.g. dropping a leading namespace prefix when fields move into a per-adapter sub-dict — are permitted as long as every output-affecting field is still represented and carries the same value.

**Rationale:** Two deployments processing the same source with different converters (or different converter configs) must produce distinct keys.
Including `converter_name` in the key is the minimal change that enables multi-converter support later.
Note: converter selection is per-deployment config, not per-job — the proposal explicitly excludes per-job converter selection from this change.

**Rollout placement:** the `compute_idempotency_key` signature change and API-side wiring belong in the same PR that replaces `JobSubmission.karakeep_id` with `source_ref` (PR 6b), not in the worker-cutover PR (PR 7).
The API is the writer of the key; updating the worker without updating the API writer would leave the API emitting old-formula keys while the worker assumes new semantics.
PR 6b therefore owns all API-side identity changes (`JobSubmission`, Source materialization, idempotency key formula, manifest `config_snapshot` writer if it is API-side).
The historical-key recomputation backfill rides with PR 6a (where it is natural alongside the `source_ref_hash` backfill) so that when PR 6b lands and the API starts emitting new-formula keys, every existing row is already under the same formula.
PR 7 narrows to worker-side fetch/convert dispatch and the runtime read-side of the new fields.

**Alternatives considered:**

- **Keep Docling-specific key formula:** blocks multi-converter support without another migration.
  Rejected.
- **Include all config fields (not just output-affecting):** over-invalidates.
  Transport-only fields (endpoint URLs, API keys) don't affect output and should not vary the key.
  The adapter controls which fields are "output-affecting" — same principle as today, but adapter-scoped.
- **Compute the key at the worker:** conflicts with the API-owns-identity decision and would require deferring the dedup gate past queue enqueue, letting duplicate submissions sit in the queue until a worker picks one up.
  Rejected.

### Decision: Preserve `karakeep_id` as a nullable convenience field on JobResponse; UI migration deferred

**Chosen:** In `JobResponse` (and equivalents that the UI consumes), keep `karakeep_id: str | None` as a nullable convenience field, populated when `source_ref.kind == "karakeep_bookmark"` and null otherwise.
The top-level `source_ref` field is the canonical identifier going forward; `karakeep_id` is a compatibility surface so the existing UI (which filters by `Bookmark.karakeep_id` at `api/routes/ui.py:98` and renders it at `ui.py:137`) continues to work against the new schema without change.
UI migration — switching the UI to consume `source_ref` directly and handle non-KaraKeep sources — is scoped to a later change.

Keep the existing response field name `url: AnyUrl | None` unchanged.
Do not rename it to `bookmark_url` — that was unintentional spec drift in earlier delta revisions.
For KaraKeep-sourced jobs, `url` is populated from the Source row's `url` as today; for other sources, `url` reflects whatever URL the fetcher/resolver chain recorded during enrichment (may be null for `InlineHtmlRef`).

**Rationale:** Removing `karakeep_id` from `JobResponse` in this change would break the in-repo UI the moment PR 6b lands.
The proposal explicitly excludes UI work, which is only consistent with preserving the field the UI reads.
Keeping `karakeep_id` as a nullable compat field until a scoped UI change lands keeps the blast radius of this refactor contained to the worker/API identity layer, which is the intent.
Request-side, `JobSubmission.karakeep_id` is still removed (callers must submit `source_ref`) because no internal consumer other than this API writes submissions; only the read-side field is preserved.

**UI search-field compatibility:** The UI's server-side search at `api/routes/ui.py:98` runs a `LIKE` match against `Bookmark.karakeep_id` alongside other job fields.
Post-rename this becomes `Source.karakeep_id`, now nullable.
The `LIKE` match continues to work: rows with `karakeep_id IS NULL` naturally fail the pattern and are excluded, and KaraKeep-backed rows match as today.
No `conversion-ui` delta is opened because the UI's public behavior is preserved; the coupling is noted here so a future UI change can find the reference point.

**Alternatives considered:**

- **Remove `karakeep_id` from both request and response; migrate UI in this change:** expands scope into HTMX templates and UI route logic, which the proposal excludes.
  Rejected.
- **Rename the URL field to `bookmark_url`:** undiscussed rename, consumer break, and semantically wrong for non-KaraKeep sources (it's just a URL).
  Rejected.

## Architecture

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│                          aizk.conversion.core                              │
│                                                                             │
│  protocols.py          types.py            source_ref.py     registry.py   │
│  ┌──────────────┐      ┌───────────────┐   ┌────────────┐   ┌───────────┐ │
│  │ ContentFetcher│      │ConversionInput│   │SourceRef   │   │FetcherReg │ │
│  │ RefResolver   │      │Conv.Artifacts │   │(discrim.   │   │ConverterReg││
│  │ Converter     │      │ContentType    │   │ union)     │   │           │ │
│  │ ResourceGuard │      └───────────────┘   └────────────┘   └───────────┘ │
│  └──────────────┘                                                           │
│                                                                             │
│  orchestrator.py                                                            │
│  ┌─────────────────────────────────────────────────────┐                    │
│  │ Orchestrator(resolve_fetcher, resolve_converter)    │                    │
│  │   _fetch(ref, depth) → ConversionInput              │                    │
│  │   process(ref, converter_name) → ConversionArtifacts│                    │
│  └─────────────────────────────────────────────────────┘                    │
└─────────────────────────────────────────────────────────────────────────────┘
         ▲ depends on (protocols only)            ▲ depends on (protocols only)
         │                                         │
┌────────┴────────────────────┐    ┌──────────────┴──────────────────────────┐
│ aizk.conversion.adapters    │    │ aizk.conversion.wiring                  │
│                              │    │                                          │
│ converters/                  │    │ build_worker_runtime(cfg)                │
│   docling.py                 │    │   - registries + GPU guard + all adapters│
│     DoclingConverter         │    │   - returns Orchestrator + ResourceGuard │
│                              │    │                                          │
│ fetchers/                    │    │ build_api_runtime(cfg)                   │
│   karakeep.py                │    │   - SubmissionCapabilities from policy   │
│     KarakeepBookmarkResolver │    │   - no converter adapters needed         │
│   arxiv.py                   │    │                                          │
│     ArxivFetcher             │    │ build_test_runtime(cfg)                  │
│   github.py                  │    │   - fake resolvers, in-memory registries │
│     GithubReadmeFetcher      │    │                                          │
│   url.py                     │    │ (sole package that imports core+adapters)│
│     UrlFetcher               │    └──────────────────────────────────────────┘
│   singlefile.py  (skeleton)  │
│     SingleFileFetcher        │
│     (not registered yet)     │
│   inline.py                  │
│     InlineContentFetcher     │
└──────────────────────────────┘

Existing layers (changes noted):
  workers/loop.py, supervision.py  →  GPU guard injected here (parent-side)
  workers/uploader.py              →  unchanged
  api/                             →  IngressSourceRef schema; IngressPolicy gate
  datamodel/source.py              →  renamed from bookmark.py
  datamodel/job.py                 →  source_ref denormalized; FK stays aizk_uuid
```

### Data flow through the pipeline

```text
Source row (aizk_uuid=<uuid>, source_ref: KarakeepBookmarkRef, source_type: "arxiv")
  │
  ▼
Job record (aizk_uuid FK → Source, source_ref denormalized)
  │
  ▼
Parent thread acquires GPU ResourceGuard
  │
  ▼
Orchestrator._fetch(ref, depth=0)       [runs in CHILD subprocess]
  │ resolve_fetcher("karakeep_bookmark") → KarakeepBookmarkResolver (RefResolver)
  │ resolver.resolve(ref) → ArxivRef(arxiv_id="2301.12345")
  │
  ▼
Orchestrator._fetch(ref, depth=1)
  │ resolve_fetcher("arxiv") → ArxivFetcher (ContentFetcher)
  │ fetcher.fetch(ref) → ConversionInput(content_type=PDF, bytes=<pdf>)
  │
  ▼
Orchestrator.process()
  │ resolve_converter(PDF, "docling") → DoclingConverter
  │ converter.convert(input) → ConversionArtifacts(markdown, figures, metadata)
  │
  ▼
Parent thread releases GPU ResourceGuard
  │
  ▼
Upload → S3
```

### KaraKeep bookmark resolution precedence

The `KarakeepBookmarkResolver` preserves today's exact precedence order from `orchestrator.py:194-224`:

```text
1. source_type == "arxiv"           → ArxivRef (PDF pipeline)
2. source_type == "github"          → GithubReadmeRef (HTML pipeline)
3. is_pdf_asset(bookmark)           → UrlRef pointing at KaraKeep asset URL (PDF pipeline)
4. is_precrawled_archive(bookmark)  → UrlRef pointing at KaraKeep asset URL (HTML pipeline)
5. has HTML content                 → UrlRef or InlineHtmlRef (HTML pipeline)
6. has text content                 → InlineHtmlRef with <html><body><pre> wrap (HTML pipeline)
7. none of the above                → BookmarkContentUnavailableError (permanent failure)
```

Within arXiv (step 1), the sub-precedence is:

- KaraKeep PDF asset present → fetch from KaraKeep (preferred; avoids arxiv.org rate limits)
- `arxiv_pdf_url` metadata field present → fetch from that URL
- Abstract page URL → resolve arxiv ID, construct PDF URL

## Risks

- **Idempotency key migration**: existing jobs have keys computed without `converter_name`.
  New keys for the same content would differ, silently breaking replay-idempotency for legacy jobs.
  **Mitigation**: the Alembic migration recomputes `idempotency_key` in place for every historical job row using the new formula (`source_ref_hash` from the backfilled Source row + `converter_name = "docling"` + today's Docling config snapshot).
  After migration, replay of pre-refactor content produces a key that matches the stored key and is correctly recognized as idempotent.
  Document the recomputation in release notes.
- **Import cycle between core and adapters**: if an adapter accidentally imports from another adapter, the boundary is violated.
  **Mitigation**: a CI lint rule (or a unit test) verifies that `core/` and each `adapters/` module have no cross-imports.
- **SQLite JSON column performance**: `source_ref` is a JSON column, not indexed.
  Filtering by `source_ref.kind` requires a JSON extract in the WHERE clause.
  **Mitigation**: not needed in this change (existing filters use `aizk_uuid`).
  Add a generated column + index when filtering by kind becomes a requirement.
- **Subprocess fork inherits the composition root's adapter state**: the child process re-imports modules after fork.
  Adapters must be fork-safe (no open connections, no thread-local state in module scope).
  **Mitigation**: DoclingConverter already works in a subprocess today; verify new adapters follow the same pattern.
  The composition root builds the orchestrator in the child after fork, not before.
- **Test coverage gap during structural move**: moving code between files can introduce import errors not caught by existing tests if coverage is uneven.
  **Mitigation**: PRs 3-4 (adapter extraction) preserve re-exports from old module paths; PR 8 (cleanup) deletes them only after cutover PR 7 has been validated.
- **InlineHtmlRef payload in JSON column**: text bookmark content is embedded in the ref, bloating the Source/job row.
  **Mitigation**: hard 64KB cap enforced by pydantic validation; text bookmarks are typically \<5KB.
  Monitor column size; migrate to staged storage if a future ref type needs larger payloads.
