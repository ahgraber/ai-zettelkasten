# Delta for pluggable-pipeline

## ADDED Requirements

### Requirement: Declare content fetching as a protocol with two roles

The system SHALL support two fetcher roles — content fetchers and ref resolvers — each with a distinct protocol.
A content fetcher SHALL accept a `SourceRef` and return a `ConversionInput` containing the fetched bytes and authoritative content type.
A ref resolver SHALL accept a `SourceRef` and return a more specific `SourceRef`, deferring byte-level fetching to a downstream content fetcher.
A ref resolver SHALL declare a class-level `resolves_to: ClassVar[frozenset[str]]` attribute enumerating every `SourceRef` kind it may emit at runtime; this declaration is the static edge set used for chain-closure validation at wiring time (see "Validate resolver chain closure at wiring time").
Role SHALL be declared at registration time (the registry exposes distinct registration entry points per role); role SHALL NOT be inferred by structural isinstance against the two protocols.

#### Scenario: Content fetcher returns bytes

- **GIVEN** a `SourceRef` whose kind maps to a registered content fetcher
- **WHEN** the fetcher is invoked
- **THEN** a `ConversionInput` is returned containing the source bytes and an authoritative `ContentType`

#### Scenario: Ref resolver returns a refined ref

- **GIVEN** a `SourceRef` whose kind maps to a registered ref resolver
- **WHEN** the resolver is invoked
- **THEN** a new `SourceRef` of a different kind is returned, to be dispatched on by the orchestrator

#### Scenario: Role is declared at registration, not inferred

- **GIVEN** a registry with distinct registration entry points for content fetchers and ref resolvers
- **WHEN** an adapter is registered
- **THEN** the registry records role from the registration call, not from structural typing checks against the adapter's methods

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

The system SHALL follow ref-resolver chains by recursively dispatching on the returned `SourceRef`, and SHALL terminate with an error if the chain exceeds a configurable depth limit (default: 3).

#### Scenario: Single-hop resolution succeeds

- **GIVEN** a ref resolver returns a `SourceRef` whose kind maps to a content fetcher
- **WHEN** the pipeline dispatches the original ref
- **THEN** the content fetcher is invoked after exactly one resolution hop

#### Scenario: Depth limit exceeded raises error

- **GIVEN** a chain of ref resolvers each returning a new `SourceRef` whose kind maps to another resolver
- **WHEN** the chain length exceeds the configured depth limit
- **THEN** a `FetcherDepthExceeded` error is raised and the job is classified as a non-retryable failure

### Requirement: Validate resolver chain closure at wiring time

The system SHALL validate, at wiring completion, that every registered resolver's declared `resolves_to` set references only kinds that are themselves registered in the fetcher registry (as either content fetchers or further resolvers).
Validation SHALL run once inside the shared `register_ready_adapters` helper, after all adapters have been registered and before the `DeploymentCapabilities` descriptor is returned.
Validation SHALL additionally assert that the declared resolver DAG contains no cycles and that no declared path exceeds the configured depth cap.
If any resolver declares a `resolves_to` kind that is not registered, wiring SHALL raise a `ChainNotTerminated` error identifying the offending resolver and the missing kind; process startup SHALL fail before any request is accepted.
This is a static, one-shot check against declared edges; it does not replace the runtime `FetcherNotRegistered` guard, which remains in force for dispatch-time faults (e.g., a resolver returning a kind outside its declared set).

#### Scenario: Closure validation passes for a terminating graph

- **GIVEN** `KarakeepBookmarkResolver` is registered with `resolves_to = {"arxiv", "github_readme", "url", "inline_html"}` and content fetchers are registered for each of those kinds
- **WHEN** `register_ready_adapters` completes
- **THEN** validation passes and the `DeploymentCapabilities` descriptor is returned

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

### Requirement: Expose a deployment capability descriptor for API gating

The system SHALL expose a `DeploymentCapabilities` descriptor — produced by the wiring layer — that answers, for a given deployment: which `SourceRef` kinds are accepted, which `ContentType` values have a registered converter, and which startup probes the deployment's adapters declare.
`accepted_kinds` SHALL be exactly the set of kinds registered in the fetcher registry (content fetchers and resolvers); no separate readiness concept is maintained.
Adapters that are not yet ready to serve SHALL NOT be registered in the registry — their implementation classes may exist in the codebase as skeletons, but the composition root does not wire them until they function.
Worker and API wiring SHALL share a single registration helper so both process roles derive the same `accepted_kinds`.
The API SHALL consult this descriptor (not raw registry membership from elsewhere in the code) when validating `source_ref.kind`.

#### Scenario: Registered kind accepted

- **GIVEN** the composition root registered a fetcher for kind `"karakeep_bookmark"` via the shared registration helper
- **WHEN** a client submits a job with `source_ref.kind = "karakeep_bookmark"`
- **THEN** the submission is accepted because `"karakeep_bookmark"` is in `DeploymentCapabilities.accepted_kinds`

#### Scenario: Unregistered kind rejected

- **GIVEN** `DeploymentCapabilities.accepted_kinds = {"karakeep_bookmark"}`
- **WHEN** a client submits a job with `source_ref.kind = "url"`
- **THEN** HTTP 422 is returned with an error indicating the kind is not supported in this deployment

#### Scenario: Not-yet-ready adapter is not registered

- **GIVEN** `SingleFileFetcher` exists as a skeleton class but is deliberately not wired by the shared registration helper
- **WHEN** `DeploymentCapabilities` is built
- **THEN** `"singlefile"` is not in `accepted_kinds`, and a submission with `source_ref.kind = "singlefile"` is rejected with HTTP 422

#### Scenario: Worker and API share accepted_kinds

- **GIVEN** worker and API wiring both invoke the shared registration helper
- **WHEN** both `DeploymentCapabilities` descriptors are built
- **THEN** their `accepted_kinds` sets are equal

### Requirement: Represent content sources as a discriminated union

The system SHALL represent the source of content to be fetched as a `SourceRef` — a pydantic discriminated union keyed on a `kind` field.
Each variant SHALL carry only the data needed to fetch its content and SHALL be serializable to and from JSON for persistence.
Each variant SHALL expose a `to_dedup_payload() -> dict` method returning a canonical, normalized representation used for identity hashing (see "Compute source_ref_hash from a canonical dedup payload").
Exception: `InlineHtmlRef` MAY embed content bytes directly, subject to a hard size cap (64KB), as a documented exception for small inline-text payloads.

#### Scenario: SourceRef round-trips through JSON

- **GIVEN** a `SourceRef` variant (e.g., `ArxivRef(arxiv_id="2301.12345")`)
- **WHEN** it is serialized to JSON and deserialized back
- **THEN** the deserialized value equals the original, with the correct variant type restored via the `kind` discriminator

#### Scenario: Unknown kind rejected on deserialization

- **GIVEN** a JSON object with a `kind` value not matching any registered variant
- **WHEN** deserialization is attempted
- **THEN** a validation error is raised

#### Scenario: InlineHtmlRef exceeding size cap rejected

- **GIVEN** an `InlineHtmlRef` whose content exceeds 64KB
- **WHEN** the model is constructed
- **THEN** a pydantic validation error is raised

### Requirement: Compute source_ref_hash from a canonical dedup payload

The system SHALL compute `source_ref_hash` by invoking each `SourceRef` variant's `to_dedup_payload()` method to obtain a canonical, normalized dict containing only the fields that define semantic identity for that variant, then hashing the JSON-encoded payload with stable key ordering and separators.
The hash SHALL NOT be derived from `model_dump_json()` of the full ref, so cosmetic changes (field ordering, default values, non-identity fields) do not produce different hashes for the same logical source.

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
