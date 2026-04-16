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
The worker needs all fetcher/converter adapters and the GPU guard.
The API needs the accepted-kinds validator and schema awareness but not converter adapters.
Tests need fake resolvers.
One monolithic `build_orchestrator()` either over-provisions (API gets converter adapters it doesn't use) or under-specifies (which probes run where).
Role-specific builders make startup probes, accepted source kinds, and registered adapters explicit per process type.

**Alternatives considered:**

- **Single `build_orchestrator(cfg)` with conditional branches:** works at small scale but accumulates `if role == ...` branches.
  Rejected because role-specific builders are just as simple and more explicit.

### Decision: Two fetcher roles — ContentFetcher and RefResolver

**Chosen:** Two distinct protocols.

- `ContentFetcher.fetch(ref) -> ConversionInput` (terminal — returns bytes).
- `RefResolver.resolve(ref) -> SourceRef` (intermediate — refines a ref into a more specific one).

The orchestrator recurses on resolved refs with a configurable depth cap (default 3).

**Role is declared at registration, not inferred:** The registry exposes two distinct registration entry points — `register_content_fetcher(kind, impl)` and `register_resolver(kind, impl)` — and stores role alongside the implementation.
Role is not inferred by structural isinstance against the two protocols, because Python protocols are structural and a single class could accidentally satisfy both.
Kind uniqueness is enforced across both roles: the same `kind` cannot have both a content fetcher and a resolver registered.

**Rationale:** Encoding the role in the type (rather than a union return type) moves the dispatch decision to registration time, not call time.
A `ContentFetcher` that accidentally returns a `SourceRef` is a static type error.
Each protocol has a single, concrete return type — no isinstance checks on the payload.

The depth cap prevents infinite resolver chains.
In practice the longest chain is 1 hop (KaraKeep bookmark -> arxiv/github/url ref).

**Chain completeness is a wiring-time responsibility, not a declared static property:** A resolver chain is only useful if every kind the chain can reach is itself registered with a handler.
Rather than encode each resolver's possible emissions as a protocol attribute and verify the graph statically, the composition root is responsible for registering the full set of reachable kinds.
If a resolver returns a ref whose kind is not registered, the orchestrator raises `FetcherNotRegistered` at runtime for that job.
In practice there is one resolver (KaraKeep), its emission set is fixed, and anyone deploying the service registers the terminal fetchers alongside it — so the runtime-catch window is narrow.
The cost of stronger static verification (a declared emissions attribute on every resolver) is not justified by the class of bugs it prevents at this scale.

**Alternatives considered:**

- **Single fetcher protocol returning `ConversionInput | SourceRef`:** union return type is too permissive; any fetcher can return either thing without static error.
  Isinstance on every return value is repetitive.
- **Fetcher delegation inside the adapter (resolver calls another fetcher internally):** hides delegation, risks unbounded recursion, requires injecting the resolver callable into adapters (more wiring, harder to test).
- **Infer role structurally via isinstance against protocols:** structural protocols can match any class with the right method shape; "no adapter SHALL implement both" is unenforceable without a declared role.
  Rejected.
- **Declare each resolver's possible emissions as a protocol attribute so the wiring layer can statically verify chain termination:** stronger check, but adds a self-describing attribute on every resolver for a class of wiring bugs that only occurs under coordinated misconfiguration of a small registered set.
  Rejected in favor of runtime `FetcherNotRegistered` and composition-root discipline.

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

**Chosen:** `InlineHtmlRef` embeds the inline HTML/text content directly in the ref, with a hard size cap (64KB).
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

### Decision: Gate accepted API kinds via a DeploymentCapabilities descriptor

**Chosen:** The wiring layer builds a `DeploymentCapabilities` descriptor; the API gates on it.

```text
DeploymentCapabilities:
  accepted_kinds: frozenset[str]                     # source_ref.kind values accepted at ingress
  content_types_for(kind) -> frozenset[ContentType]  # terminal content types this kind can produce
  converter_available(content_type) -> bool          # a converter is registered for this content_type
  startup_probes: list[Probe]                        # adapter-declared probes
```

`accepted_kinds` is exactly the set of kinds the wiring layer has registered (content fetchers and resolvers).
Stubs that are not ready are simply not registered — their class files may live in the codebase as skeletons for future work, but the composition root does not wire them into the registry until they function.
This collapses the earlier `is_ready(kind)` / declared-accepted-kinds concepts: there is no difference between "registered" and "ready" because registration is the commitment to serve the kind.

Worker and API wiring share a single `register_ready_adapters(registry, cfg)` helper so the two process roles cannot drift on which kinds are accepted.

**Rationale:** "Registered but not ready" is a distinction without a purpose: if a kind is not ready to serve, the API should not accept it, and the cleanest way to encode that is to not put it in the registry.
Keeping a stub in the registry only to filter it back out via `is_ready` inverts the meaning of registration and requires two sets (registered, accepted) to stay in sync.

**Rollout:** PR 6 wires only the KaraKeep resolver and its emission terminals that are ready (or none, if earlier); `accepted_kinds` reflects what was registered.
PR 7 widens by registering additional adapters as they land.
Incremental rollout is preserved without a readiness flag.

**Alternatives considered:**

- **Keep an `is_ready(kind)` concept and a separate declared-accepted-kinds set:** requires the composition root to maintain two lists that must stay in sync.
  Rejected — "don't register it" is the simpler expression of the same intent.
- **Gate on `FetcherRegistry.kinds()` only, with no descriptor at all:** viable, but the descriptor is still useful for converter availability, startup probes, and future admin introspection.
  Keep the descriptor; drop the readiness layer.
- **Merge PR 6 and PR 7 into one atomic deploy:** simpler but loses the incremental rollout advantage and makes the PR larger.
  Acceptable for a sole user but less reviewable.
- **Let invalid jobs fail at worker fetch time:** technically works (the job gets FAILED_PERM) but wastes a queue slot and is confusing.
  Rejected.

### Decision: Manifest evolution — version bump and nullable/relocated provenance

**Chosen:** Bump `ConversionManifest.version` from `"1.0"` to `"2.0"`.

- The manifest carries a typed `provenance` block that records the **terminal fetch state** — the identity of the ref whose `ContentFetcher` actually produced the converted bytes.
  This answers "what document was this?"
  for the converted output.
  Keyed on the terminal ref's `kind`:
  - `{"kind": "url", "url": "..."}` for jobs that terminated at a direct URL fetch.
  - `{"kind": "arxiv", "arxiv_id": "..."}` / `{"kind": "github_readme", "owner": "...", "repo": "..."}` for jobs resolved from a KaraKeep bookmark (or submitted directly) that terminated at these fetchers.
  - `{"kind": "inline_html", "content_hash": "..."}` for inline-text jobs.
  - `{"kind": "karakeep_bookmark", "bookmark_id": "..."}` only if the KaraKeep bookmark was itself the terminal fetch (i.e., the content bytes came from KaraKeep itself — today this happens for PDF-asset and precrawled-archive bookmarks where KaraKeep is the byte source).
- An optional `ingress` block records the ref the submitter provided, when it differs from the terminal ref — e.g., a KaraKeep bookmark that resolved to arxiv emits `{"provenance": {"kind": "arxiv", "arxiv_id": "..."}, "ingress": {"kind": "karakeep_bookmark", "bookmark_id": "..."}}`.
  When ingress equals the terminal ref (direct URL submission, direct arxiv submission, etc.), `ingress` is omitted.
  This preserves the bookmark id for KaraKeep-sourced jobs without losing the terminal identity.
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
A version bump makes the break explicit, the typed provenance block keeps provenance semantically meaningful across all source types, and nullable URL/title fields remove the KaraKeep assumption from the output contract.

**Alternatives considered:**

- **Keep manifest version 1.0, make `karakeep_id` Optional:** silent contract change, consumers of existing manifests (or future downstream readers) cannot tell v1-with-new-semantics from v1-classic.
  Rejected — version bumps exist precisely for this.
- **Per-kind provenance as flat optional fields at manifest root (`arxiv_id`, `github_owner`, etc.):** clutters the top level and requires readers to know which fields correlate with which kind.
  Typed nested block is cleaner.
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

**Key stability — intentionally broken:** Post-refactor keys do not match pre-refactor keys for the same content.
Two inputs change: `source_ref_hash` replaces the old `aizk_uuid`-based key component, and `converter_name` is newly incorporated.
The pre-existing `idempotency_key` column values continue to exist on historical rows; they are not recomputed.
Going forward, all new submissions use the new formula.
Duplicate submissions of a pre-refactor source will compute a new-formula key that does not collide with the stored pre-refactor key, so the legacy job is not considered an idempotent replay of a post-refactor submission — this is acceptable because post-refactor submissions carry different identity anyway (`source_ref` vs `karakeep_id`).

**Snapshot structural equivalence (separate from hash equivalence):** The Docling adapter's `config_snapshot()` SHALL contribute the same field set as today's Docling-specific config hash — including `picture_description_enabled` and any other output-affecting values the pre-refactor formula included.
This is a structural contract on the snapshot dict (same keys, same values for the same input config), not a claim that the final hash matches.

**Rationale:** Two deployments processing the same source with different converters (or different converter configs) must produce distinct keys.
Including `converter_name` in the key is the minimal change that enables multi-converter support later.
Note: converter selection is per-deployment config, not per-job — the proposal explicitly excludes per-job converter selection from this change.

**Rollout placement:** this change belongs in the same PR that replaces `JobSubmission.karakeep_id` with `source_ref` (PR 6), not in the worker-cutover PR (PR 7).
The API is the writer of the key; updating the worker without updating the API writer would leave the API emitting old-formula keys while the worker assumes new semantics.
PR 6 therefore owns all API-side identity changes (`JobSubmission`, Source materialization, idempotency key formula, manifest `config_snapshot` writer if it is API-side).
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

**Rationale:** Removing `karakeep_id` from `JobResponse` in this change would break the in-repo UI the moment PR 6 lands.
The proposal explicitly excludes UI work, which is only consistent with preserving the field the UI reads.
Keeping `karakeep_id` as a nullable compat field until a scoped UI change lands keeps the blast radius of this refactor contained to the worker/API identity layer, which is the intent.
Request-side, `JobSubmission.karakeep_id` is still removed (callers must submit `source_ref`) because no internal consumer other than this API writes submissions; only the read-side field is preserved.

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
│   karakeep.py                │    │   - accepted-kinds set from registry     │
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
  api/                             →  source_ref schema; accepted-kinds gate
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
  New keys for the same content will differ.
  **Mitigation**: this is acceptable — the change is intentional (different converter -> different output -> different key).
  Document in release notes.
  Backfill script sets `converter_name = "docling"` for existing job records.
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
