# Pluggable Pipeline Specification

> Created from delta specs on 2026-04-20
> Source: .specs/changes/pluggable-fetch-convert/specs/pluggable-pipeline/spec.md

## Purpose

The pluggable pipeline defines the core protocols, registries, and composition contracts that allow fetchers, resolvers, and converters to be swapped, extended, and validated at wiring time without modifying the orchestrator.
It separates adapter declaration from deployment policy and enforces structural invariants through a single composition root (the wiring package).

## Requirements

### Requirement: Declare content fetching as a protocol with two roles

The system SHALL support two fetcher roles — content fetchers and ref resolvers — each with a distinct protocol.
A content fetcher SHALL accept a `SourceRef` and return a `ConversionInput` containing the fetched bytes and authoritative content type.
A content fetcher SHALL declare a class-level `produces: ClassVar[frozenset[ContentType]]` attribute enumerating every `ContentType` it may emit at runtime; wiring reads this attribute off the adapter class so the set of terminal content types is an adapter-owned declaration rather than a separate map in the wiring layer.
A ref resolver SHALL accept a `SourceRef` and return a more specific `SourceRef`, deferring byte-level fetching to a downstream content fetcher.
A ref resolver SHALL declare a class-level `resolves_to: ClassVar[frozenset[str]]` attribute enumerating every `SourceRef` kind it may emit at runtime; this declaration is the static edge set used for chain-closure validation at wiring time (see "Validate resolver chain closure at wiring time").
Fetcher adapter classes SHALL NOT carry any class-level flag indicating whether their kind is publicly submittable; public-ingress acceptability is a deployment policy (see "Expose a submission capability descriptor for API gating"), not an adapter property.
Registration entry points (`register_content_fetcher` and `register_resolver`) declare caller intent at the registration call-site AND validate structural conformance at registration time: `register_content_fetcher(kind, impl)` SHALL raise a typed registration error if `isinstance(impl, RefResolver)` is true or if `impl` does not satisfy the `ContentFetcher` protocol; `register_resolver(kind, impl)` SHALL raise a typed registration error if `impl` does not satisfy the `RefResolver` protocol.
Dispatch-time role SHALL be determined structurally via `isinstance(impl, RefResolver)` against the `@runtime_checkable` protocol so the orchestrator does not carry a separate role tag.
Because both the registration-time check and the dispatch-time check read structural conformance from the same runtime-checkable protocol, the declared intent (which entry point was called) and the runtime role (which branch the orchestrator takes) cannot diverge.

#### Scenario: Content fetcher returns bytes

- **GIVEN** a `SourceRef` whose kind maps to a registered content fetcher
- **WHEN** the fetcher is invoked
- **THEN** a `ConversionInput` is returned containing the source bytes and an authoritative `ContentType`

#### Scenario: Content fetcher declares produced content types

- **GIVEN** a content fetcher adapter class
- **WHEN** its `produces` class attribute is inspected
- **THEN** it returns a non-empty `frozenset[ContentType]` enumerating every terminal type the fetcher may emit (e.g., `{ContentType.PDF, ContentType.HTML}` for a URL fetcher)

#### Scenario: Ref resolver returns a refined ref

- **GIVEN** a `SourceRef` whose kind maps to a registered ref resolver
- **WHEN** the resolver is invoked
- **THEN** a new `SourceRef` of a different kind is returned, to be dispatched on by the orchestrator

#### Scenario: Dispatch role is determined structurally at runtime

- **GIVEN** a registered adapter and a `SourceRef` whose kind maps to it
- **WHEN** the orchestrator dispatches the ref
- **THEN** it invokes `impl.resolve(ref)` when `isinstance(impl, RefResolver)` is true and `impl.fetch(ref)` otherwise

#### Scenario: register_content_fetcher rejects a resolver impl

- **GIVEN** an adapter class that satisfies the `RefResolver` protocol (exposes `resolve` + `resolves_to`)
- **WHEN** `register_content_fetcher(kind, impl)` is called with that adapter
- **THEN** a typed registration error is raised and the registry state is unchanged

#### Scenario: register_resolver rejects a non-resolver impl

- **GIVEN** an adapter class that does not satisfy the `RefResolver` protocol (missing `resolve` or `resolves_to`)
- **WHEN** `register_resolver(kind, impl)` is called with that adapter
- **THEN** a typed registration error is raised and the registry state is unchanged

#### Scenario: Resolver declares its output kinds

- **GIVEN** a `RefResolver` implementation registered for kind `"karakeep_bookmark"`
- **WHEN** its `resolves_to` class attribute is inspected
- **THEN** it returns a non-empty `frozenset[str]` enumerating every `SourceRef` kind it may emit (e.g., `{"arxiv", "github_readme", "url", "inline_html"}`)

### Requirement: Declare document conversion as a capability-indexed protocol

The system SHALL support a converter protocol where each converter implementation declares the set of content types it can handle and whether it requires GPU admission control.
A converter SHALL accept a `ConversionInput` and return `ConversionArtifacts`.
The set of supported formats SHALL be a static property of the converter class, not determined at call time.
Each converter SHALL declare a static `requires_gpu: bool` attribute; the orchestrator SHALL use this declaration to decide whether to acquire the GPU `ResourceGuard` for the job (see "Enforce GPU admission control above the subprocess boundary").

#### Scenario: Converter declares supported formats

- **GIVEN** a converter adapter registered with the pipeline
- **WHEN** its capabilities are inspected
- **THEN** it reports a non-empty set of `ContentType` values it can convert

#### Scenario: Converter declares GPU requirement

- **GIVEN** a converter adapter registered with the pipeline
- **WHEN** its `requires_gpu` attribute is inspected
- **THEN** it returns a boolean indicating whether the converter needs GPU admission control

#### Scenario: Converter produces artifacts from supported input

- **GIVEN** a `ConversionInput` whose content type is in the converter's supported set
- **WHEN** the converter is invoked
- **THEN** `ConversionArtifacts` are returned containing the converted output and any extracted assets

### Requirement: Resolve fetchers by source ref kind via a registry

The system SHALL maintain a fetcher registry mapping each `SourceRef` kind to exactly one adapter — either a content fetcher or a ref resolver, but not both.
The registry SHALL expose distinct registration entry points per role (`register_content_fetcher(kind, impl)` and `register_resolver(kind, impl)`).
Kind uniqueness SHALL be enforced across both roles: a kind with a content fetcher registered cannot also have a resolver registered, and vice versa.
The registry SHALL reject duplicate registration for a kind that is already registered in either role.
The registry SHALL raise a typed error when resolution is attempted for an unregistered kind.
The registry SHALL expose `registered_kinds() -> frozenset[str]` returning every kind present across both roles.
Worker dispatch operates on `registered_kinds()`.
Public-ingress acceptability is NOT derived from registry membership — see "Expose a submission capability descriptor for API gating".

#### Scenario: Fetcher resolved by kind

- **GIVEN** a fetcher is registered for kind `"arxiv"`
- **WHEN** resolution is requested for a `SourceRef` with kind `"arxiv"`
- **THEN** the registered fetcher is returned

#### Scenario: Duplicate kind registration rejected

- **GIVEN** a fetcher is already registered for kind `"arxiv"`
- **WHEN** a second fetcher attempts to register for kind `"arxiv"`
- **THEN** a registration error is raised

#### Scenario: Unregistered kind raises typed error

- **GIVEN** no fetcher is registered for kind `"unknown"`
- **WHEN** resolution is requested for that kind
- **THEN** a `FetcherNotRegistered` error is raised

### Requirement: Resolve converters by content type and implementation name via a registry

The system SHALL maintain a converter registry that indexes converters by `(content_type, implementation_name)`.
When a converter supporting multiple content types is registered, it SHALL be resolvable for each of those types independently.
The registry SHALL raise a typed error when no converter is registered for the requested combination.

#### Scenario: Converter resolved by content type and name

- **GIVEN** a converter named `"docling"` is registered with `supported_formats = {pdf, html}`
- **WHEN** resolution is requested for `(pdf, "docling")`
- **THEN** the registered converter is returned

#### Scenario: No converter for content type raises typed error

- **GIVEN** no converter is registered for content type `image`
- **WHEN** resolution is requested for `(image, "docling")`
- **THEN** a `NoConverterForFormat` error is raised and the error is classified as non-retryable

### Requirement: Bound resolver delegation depth

The system SHALL follow ref-resolver chains by recursively dispatching on the returned `SourceRef`, and SHALL terminate with an error if the chain exceeds a configurable depth limit (default: 2).
The `FetcherDepthExceeded` error message SHALL include the configured cap, the sequence of `SourceRef` kinds traversed, and the configuration key used to raise the cap, so that an operator encountering a legitimately longer chain can respond by reconfiguring rather than reading source code.

#### Scenario: Single-hop resolution succeeds

- **GIVEN** a ref resolver returns a `SourceRef` whose kind maps to a content fetcher
- **WHEN** the pipeline dispatches the original ref
- **THEN** the content fetcher is invoked after exactly one resolution hop

#### Scenario: Depth limit exceeded raises error

- **GIVEN** a chain of ref resolvers each returning a new `SourceRef` whose kind maps to another resolver
- **WHEN** the chain length exceeds the configured depth limit
- **THEN** a `FetcherDepthExceeded` error is raised, the job is classified as a non-retryable failure, and the error message includes the configured cap, the sequence of kinds traversed, and the configuration key used to raise the cap

### Requirement: Validate resolver chain closure at wiring time

The system SHALL validate, at wiring completion, that every registered resolver's declared `resolves_to` set references only kinds that are themselves registered in the fetcher registry (as either content fetchers or further resolvers).
Validation SHALL run once inside the shared `register_ready_adapters` helper, after all adapters have been registered and before capability descriptors are returned.
Validation operates on `FetcherRegistry.registered_kinds()` — the full worker-dispatch set — not on any public-ingress subset.
Validation SHALL additionally assert that the declared resolver DAG contains no cycles and that no declared path exceeds the configured depth cap.
If any resolver declares a `resolves_to` kind that is not registered, wiring SHALL raise a `ChainNotTerminated` error identifying the offending resolver and the missing kind; process startup SHALL fail before any request is accepted.
This is a static, one-shot check against declared edges; it does not replace the runtime `FetcherNotRegistered` guard, which remains in force for dispatch-time faults (e.g., a resolver returning a kind outside its declared set).

#### Scenario: Closure validation passes for a terminating graph

- **GIVEN** `KarakeepBookmarkResolver` is registered with `resolves_to = {"arxiv", "github_readme", "url", "inline_html"}` and content fetchers are registered for each of those kinds
- **WHEN** `register_ready_adapters` completes
- **THEN** validation passes and wiring returns successfully

#### Scenario: Missing downstream kind fails at wiring

- **GIVEN** a resolver declares `resolves_to = {"arxiv"}` but no adapter is registered for kind `"arxiv"`
- **WHEN** `register_ready_adapters` runs closure validation
- **THEN** a `ChainNotTerminated` error is raised naming the resolver and the missing kind, and the process fails to start

#### Scenario: Declared cycle fails at wiring

- **GIVEN** two resolvers whose `resolves_to` sets form a cycle (A → B → A)
- **WHEN** `register_ready_adapters` runs closure validation
- **THEN** a `ChainNotTerminated` error is raised identifying the cycle

### Requirement: Inject fetcher and converter resolution into the orchestrator

The orchestrator SHALL receive its fetcher resolver and converter resolver as constructor dependencies, and SHALL not import or reference any concrete adapter module.

#### Scenario: Orchestrator operates with injected fakes

- **GIVEN** an orchestrator constructed with fake resolver callables returning in-memory fetchers and converters
- **WHEN** a job is processed
- **THEN** the orchestrator completes the fetch-convert cycle using only the injected fakes, with no dependency on real adapters or registries

#### Scenario: Orchestrator has no transitive import of adapter modules

- **GIVEN** the orchestrator's module source
- **WHEN** its import graph is inspected
- **THEN** no adapter module (e.g., docling, karakeep, arxiv) appears in the transitive closure

### Requirement: Enforce GPU admission control above the subprocess boundary

The system SHALL bound the number of GPU-consuming conversion subprocesses running concurrently via a GPU `ResourceGuard` acquired in the parent process before subprocess spawn.
The guard SHALL be a context manager implemented by a threading primitive shared across the parent's worker thread pool.
The orchestrator SHALL acquire the guard if and only if the dispatched converter declares `requires_gpu == True`; converters declaring `requires_gpu == False` SHALL spawn without contending on the GPU guard.
The acquiring worker thread SHALL be the sole releaser: the guard SHALL be held for the full subprocess lifecycle (spawn, supervise, reap) and SHALL be released when the acquiring thread's `with` block unwinds — whether conversion succeeded, the child crashed, the supervision loop raised, or the parent cancelled.
The supervision loop SHALL NOT release the guard on behalf of the acquiring thread; its role is to detect child termination and return control so the acquiring thread's `with` block unwinds.
Converter adapters running inside forked child processes SHALL NOT own or acquire the cross-job GPU guard.

#### Scenario: GPU-consuming converter acquires guard

- **GIVEN** a job dispatched to a converter whose `requires_gpu == True`
- **WHEN** the worker prepares to spawn the conversion subprocess
- **THEN** the worker thread enters the GPU guard's `with` block before spawning the subprocess

#### Scenario: Non-GPU converter bypasses guard

- **GIVEN** a (hypothetical) converter whose `requires_gpu == False`
- **WHEN** a job is dispatched to it
- **THEN** the subprocess is spawned without acquiring the GPU guard, and GPU-bound jobs on other threads are not blocked by it

#### Scenario: Parent-side guard limits concurrent GPU subprocesses

- **GIVEN** the GPU concurrency limit is 1 and one worker thread has acquired the guard and spawned a GPU-consuming conversion subprocess
- **WHEN** a second worker thread attempts to spawn a GPU-consuming conversion subprocess
- **THEN** the second thread blocks on the guard until the first thread's subprocess completes and the guard is released

#### Scenario: Guard held through subprocess reap

- **GIVEN** a worker thread acquires the GPU guard and spawns a conversion subprocess
- **WHEN** the subprocess exits (successfully or via crash) and supervision returns
- **THEN** the guard remains held until the acquiring thread's `with` block unwinds after reap (not released at spawn or at crash detection)

#### Scenario: Guard released on subprocess crash via acquiring thread

- **GIVEN** a worker thread holds the GPU guard and its conversion subprocess crashes
- **WHEN** the supervision loop detects the failure and returns control to the acquiring thread
- **THEN** the acquiring thread's `with` block unwinds and releases the guard; other threads may proceed

### Requirement: Wire adapters via role-specific builders

The system SHALL assemble registries, adapters, resource guards, and the orchestrator via role-specific builder functions in a single wiring package.
Each builder SHALL register only the adapters, probes, and accepted source kinds appropriate for its process role.
The wiring package SHALL be the only package that imports both core protocols and concrete adapter implementations.

#### Scenario: Worker builder registers all adapters

- **GIVEN** a worker process starting up
- **WHEN** `build_worker_runtime(cfg)` is called
- **THEN** all fetcher and converter adapters are registered, the GPU guard is created, and the orchestrator is fully wired

#### Scenario: API builder provides accepted-kinds set

- **GIVEN** an API process starting up
- **WHEN** `build_api_runtime(cfg)` is called
- **THEN** the accepted source-ref kinds are derived from the fetcher registry and made available for request validation

#### Scenario: Wiring package is the sole cross-cutting import site

- **GIVEN** the project's import graph
- **WHEN** adapter modules are traced as importers
- **THEN** only the wiring package imports both core and adapter packages

### Requirement: Expose a deployment capability descriptor for worker dispatch

The system SHALL expose a `DeploymentCapabilities` descriptor — produced by worker wiring — describing the worker-side capabilities of the running deployment: which `SourceRef` kinds the worker can dispatch (full `FetcherRegistry.registered_kinds()`, spanning both resolvers and content fetchers), which `ContentType` values have a registered converter, and which startup probes the registered adapters declare.
Adapters that are not yet ready to serve SHALL NOT be registered in the registry — their implementation classes may exist in the codebase as skeletons, but the composition root does not wire them until they function.
`DeploymentCapabilities` is consumed by the worker for dispatch and by observability/introspection surfaces; it is NOT the authority for public-ingress acceptability.

#### Scenario: Worker descriptor reports registered dispatch kinds

- **GIVEN** worker wiring has registered `KarakeepBookmarkResolver` and content fetchers for `"arxiv"`, `"github_readme"`, `"url"`, and `"inline_html"`
- **WHEN** `DeploymentCapabilities` is built
- **THEN** `registered_kinds` contains `"karakeep_bookmark"`, `"arxiv"`, `"github_readme"`, `"url"`, `"inline_html"` — every kind the orchestrator can dispatch

#### Scenario: Not-yet-ready adapter is not registered

- **GIVEN** `SingleFileFetcher` exists as a skeleton class but is deliberately not wired by the shared registration helper
- **WHEN** `DeploymentCapabilities` is built
- **THEN** `"singlefile"` is not in `registered_kinds`

### Requirement: Expose a submission capability descriptor for API gating

The system SHALL expose a `SubmissionCapabilities` descriptor — produced by API wiring — that answers "is this `source_ref.kind` publicly submittable in this deployment?"
Public-ingress policy is a deployment concern, distinct from worker dispatch and distinct from future not-yet-wired kinds.
`SubmissionCapabilities` SHALL expose `accepted_submission_kinds: frozenset[str]` sourced from an `IngressPolicy` configuration value (not from registry membership and not from adapter class attributes).
The `IngressPolicy.accepted_submission_kinds` SHALL be a subset of `FetcherRegistry.registered_kinds()`; wiring SHALL raise a typed configuration error at startup if the policy references a kind that is not registered.
The API SHALL consult `SubmissionCapabilities` (not `DeploymentCapabilities` and not raw registry membership) when validating `source_ref.kind`.
Worker and API do NOT share identical accepted kind sets by design: the worker dispatches every registered kind produced by the resolver chain; the API accepts only the subset the deployment has opted to expose publicly.
At cutover, `IngressPolicy.accepted_submission_kinds` SHALL contain exactly `{"karakeep_bookmark"}`; widening the set is a future configuration change, not an adapter change.

#### Scenario: Publicly submittable kind accepted at ingress

- **GIVEN** `IngressPolicy.accepted_submission_kinds` contains `"karakeep_bookmark"` and the worker registry has `"karakeep_bookmark"` registered
- **WHEN** a client submits a job with `source_ref.kind = "karakeep_bookmark"`
- **THEN** the submission is accepted because `"karakeep_bookmark"` is in `SubmissionCapabilities.accepted_submission_kinds`

#### Scenario: Worker-internal dispatch kind rejected at ingress

- **GIVEN** `"url"` is registered in the worker's `FetcherRegistry` (as a resolver target or a submittable future kind) but is NOT in `IngressPolicy.accepted_submission_kinds`
- **WHEN** a client submits a job with `source_ref.kind = "url"`
- **THEN** HTTP 422 is returned with an error indicating the kind is not publicly submittable in this deployment, even though the worker can dispatch it

#### Scenario: IngressPolicy references an unregistered kind

- **GIVEN** `IngressPolicy.accepted_submission_kinds` contains `"singlefile"` but `SingleFileFetcher` is not registered in the worker's `FetcherRegistry`
- **WHEN** API wiring is built
- **THEN** a typed configuration error is raised at startup identifying the policy kind that lacks a registered adapter; process startup fails before any request is accepted

#### Scenario: Worker and API accepted sets diverge by design

- **GIVEN** the worker has `{"karakeep_bookmark", "arxiv", "github_readme", "url", "inline_html"}` registered and `IngressPolicy.accepted_submission_kinds = {"karakeep_bookmark"}`
- **WHEN** both descriptors are built
- **THEN** `DeploymentCapabilities.registered_kinds` is the full set and `SubmissionCapabilities.accepted_submission_kinds` is `{"karakeep_bookmark"}`; the two are intentionally not equal

### Requirement: Represent content sources as a discriminated union

The system SHALL represent the source of content to be fetched as a `SourceRef` — a pydantic discriminated union keyed on a `kind` field.
Each variant SHALL carry only the data needed to fetch its content and SHALL be serializable to and from JSON for persistence.
Each variant SHALL expose a `to_dedup_payload() -> dict` method returning a canonical, normalized representation used for identity hashing (see "Compute source_ref_hash from a canonical dedup payload").
Exception: `InlineHtmlRef` MAY embed content bytes directly, subject to a hard size cap of 64 KiB measured on the raw body bytes (not the serialized JSON form), as a documented exception for small inline-text payloads.
Serialized-JSON bloat from escaping is accepted; typical expansion is ~1.3× for HTML-shaped content.

Variants MAY carry cosmetic or forward-compatibility fields that are excluded from `to_dedup_payload()` and MAY also be ignored by the current fetcher implementation.
At cutover the following fields are accepted at deserialization but intentionally non-load-bearing:

- `ArxivRef.arxiv_pdf_url` — cosmetic fetcher hint preserved for observability; does not affect fetch behavior or identity.
- `GithubReadmeRef.branch` — accepted for forward compatibility but currently ignored by `GithubReadmeFetcher`, which hardcodes a `main`/`master` fallback.
  Wiring branch through to the fetcher is deferred until `IngressPolicy` widens to admit `github_readme` for public submission.

Accepted-but-ignored fields SHALL be documented both on the model (docstring) and in this specification so that consumers are not misled into believing the field is load-bearing at cutover.

#### Scenario: Accepted-but-ignored field round-trips without affecting identity

- **GIVEN** two `GithubReadmeRef` instances with identical `owner` and `repo` but different `branch` values (one `"main"`, one `"develop"`)
- **WHEN** `source_ref_hash` is computed for each
- **THEN** the hashes are identical because `branch` is excluded from `to_dedup_payload()`

#### Scenario: SourceRef round-trips through JSON

- **GIVEN** a `SourceRef` variant (e.g., `ArxivRef(arxiv_id="2301.12345")`)
- **WHEN** it is serialized to JSON and deserialized back
- **THEN** the deserialized value equals the original, with the correct variant type restored via the `kind` discriminator

#### Scenario: Unknown kind rejected on deserialization

- **GIVEN** a JSON object with a `kind` value not matching any registered variant
- **WHEN** deserialization is attempted
- **THEN** a validation error is raised

#### Scenario: InlineHtmlRef exceeding size cap rejected

- **GIVEN** an `InlineHtmlRef` whose raw body bytes exceed 64 KiB
- **WHEN** the model is constructed
- **THEN** a pydantic validation error is raised (the check is against raw body length, not serialized JSON length)

### Requirement: Compute source_ref_hash from a canonical dedup payload

The system SHALL compute `source_ref_hash` by invoking each `SourceRef` variant's `to_dedup_payload()` method to obtain a canonical, normalized dict containing only the fields that define semantic identity for that variant, then hashing the JSON-encoded payload with stable key ordering and separators.
The hash SHALL NOT be derived from `model_dump_json()` of the full ref, so cosmetic changes (field ordering, default values, non-identity fields) do not produce different hashes for the same logical source.

The canonical dedup payload constitutes a versioned identity contract.
Any change that alters the `to_dedup_payload()` output for previously-accepted refs (key rename, normalization rule change, field added to or removed from the payload) SHALL be treated as a breaking change to Source identity and SHALL be accompanied by a data migration that recomputes `source_ref_hash` for affected rows.
Additive `SourceRef` fields that do not participate in `to_dedup_payload()` are not breaking.
A fixture-lock test SHALL pin a curated set of `(variant_instance, expected_sha256)` pairs so accidental drift in `to_dedup_payload()` output (e.g., whitespace, key ordering, normalization) fails CI before shipping.

#### Scenario: Dedup payload fixture-lock guards against accidental drift

- **GIVEN** a curated fixture of `SourceRef` instances with their expected `source_ref_hash` values (one per variant, each including at least one non-trivial normalization case)
- **WHEN** `compute_source_ref_hash` is run against each fixture instance
- **THEN** the computed hash equals the pinned expected hash; a change to `to_dedup_payload()` that alters any output fails this test and signals a breaking-identity change requiring a migration

#### Scenario: Equivalent refs produce identical hash

- **GIVEN** two `ArxivRef` instances with the same `arxiv_id` but differing cosmetic fields (e.g., `arxiv_pdf_url` present vs. absent)
- **WHEN** `source_ref_hash` is computed for each
- **THEN** the hashes are identical

#### Scenario: Identity-defining field differs

- **GIVEN** two `ArxivRef` instances with different `arxiv_id` values
- **WHEN** `source_ref_hash` is computed for each
- **THEN** the hashes differ

#### Scenario: InlineHtmlRef hash is content-addressed

- **GIVEN** two `InlineHtmlRef` instances with identical body bytes
- **WHEN** `source_ref_hash` is computed for each
- **THEN** the hashes are identical (payload hashes the body content, not metadata)

### Requirement: Enumerate supported content types as a closed set

The system SHALL define a `ContentType` enumeration containing the content types the pipeline can handle or is expected to handle: `pdf`, `html`, `image`, `docx`, `pptx`, `xlsx`, `csv`.
Converter registration and `ConversionInput` SHALL reference values from this enumeration.

#### Scenario: ConversionInput carries a ContentType value

- **GIVEN** a fetcher returns a `ConversionInput`
- **WHEN** the content type is inspected
- **THEN** it is a member of the `ContentType` enumeration

#### Scenario: Converter registration uses ContentType values

- **GIVEN** a converter declares `supported_formats`
- **WHEN** the declaration is inspected
- **THEN** every element is a `ContentType` member

## Technical Notes

- **Implementation**: `src/aizk/conversion/core/`, `src/aizk/conversion/adapters/`, `src/aizk/conversion/wiring/`
- **Core protocols**: `aizk/conversion/core/protocols.py` — `ContentFetcher`, `RefResolver`, `Converter`, `ResourceGuard`; all fetcher/resolver protocols are `@runtime_checkable`
- **Registries**: `aizk/conversion/core/registry.py` — `FetcherRegistry` (role-aware, kind-unique), `ConverterRegistry` (indexed by `(content_type, name)`)
- **Core types**: `aizk/conversion/core/types.py` — `ContentType` enum (7 members), `ConversionInput`, `ConversionArtifacts`, `SOURCE_TYPE_BY_KIND`
- **SourceRef union**: `aizk/conversion/core/source_ref.py` — `SourceRef` pydantic discriminated union (6 variants); `compute_source_ref_hash(ref)` — SHA-256 of `json.dumps(ref.to_dedup_payload(), sort_keys=True, separators=(",", ":"))`
- **Adapters**: `aizk/conversion/adapters/fetchers/` (karakeep, arxiv, github, url, inline, singlefile-skeleton), `aizk/conversion/adapters/converters/docling.py`
- **Wiring**: `aizk/conversion/wiring/` — `build_worker_runtime`, `build_api_runtime`, `register_ready_adapters`, `validate_chain_closure`; sole import site for both core and adapters
- **GPU guard**: `threading.BoundedSemaphore` wrapped as `ResourceGuard` context manager; BoundedSemaphore chosen over plain Semaphore so extra release raises immediately rather than silently incrementing the counter
- **Chain closure validation**: runs after `register_ready_adapters` completes; walks `resolves_to` edges against `registered_kinds()`, asserts no cycles and no path exceeds depth cap; raises `ChainNotTerminated` on violation
- **IngressPolicy**: `accepted_submission_kinds: frozenset[str]` default `{"karakeep_bookmark"}`; validated as a subset of `registered_kinds()` at wiring time; widening is a config-only change
