# Pluggable Pipeline Specification

> Created from delta specs on 2026-04-20
> Source: .specs/changes/pluggable-fetch-convert/specs/pluggable-pipeline/spec.md

## Purpose

The pluggable pipeline defines the core protocols, registries, and composition contracts that allow fetchers, resolvers, and converters to be swapped, extended, and validated at wiring time without modifying the coordinator.
It separates adapter declaration from deployment policy and enforces structural invariants through a single composition root (the wiring package).

## Requirements

### Requirement: Carry source observations as a typed metadata channel through the pipeline

The system SHALL define a `SourceMetadata` value type that propagates source-descriptive fields — initially `source_url: str | None`, `normalized_url: str | None`, `document_base_url: str | None`, and `resolver_title: str | None` — from the resolver stage through the fetcher and converter stages without loss.

`SourceMetadata` SHALL be an immutable value type with optional fields, addable without breaking existing adapters.

A `SourceMetadata` value SHALL exist for every job from the resolver stage onward; when the job has no resolver, the coordinator SHALL synthesise a `SourceMetadata()` with all fields `None` before invoking the fetcher.

`SourceMetadata` SHALL define an explicit `merge(other) -> SourceMetadata` operation with field-wise "earlier non-None wins" semantics: for each field, the result is `self.<field>` if non-`None`, else `other.<field>`.
This SHALL be the only mechanism by which a stage combines its observations with prior observations.

A pipeline stage SHALL NOT silently overwrite a non-`None` `SourceMetadata` field with a `None` or replacement value via direct assignment; enrichment goes through `merge()`.

#### Scenario: Metadata exists for non-resolver job

- **GIVEN** a job submitted with a `SourceRef` whose kind has no registered resolver
- **WHEN** the coordinator dispatches to the fetcher
- **THEN** a `SourceMetadata` value with all fields `None` is passed alongside the ref

#### Scenario: Resolver observation reaches the converter unchanged

- **GIVEN** a resolver returns a `SourceMetadata` with `source_url`, `document_base_url`, and `resolver_title` set
- **WHEN** the fetcher and converter run
- **THEN** the converter observes the same values for those three fields on `input.source_meta`

#### Scenario: Merge preserves earlier non-None fields

- **GIVEN** a `SourceMetadata` with `source_url = "https://a"` and `resolver_title = None`, merged with another carrying `source_url = "https://b"` and `resolver_title = "T"`
- **WHEN** `merge()` is called
- **THEN** the result carries `source_url = "https://a"` and `resolver_title = "T"`

#### Scenario: Optional fields extend without breaking adapters

- **GIVEN** a new optional field is added to `SourceMetadata`
- **WHEN** an existing adapter that does not set the new field runs
- **THEN** the field is `None` and no adapter behaviour changes

### Requirement: Define a typed subprocess metadata schema for the conversion IPC bridge

The system SHALL define a typed `SubprocessMetadata` model that is the sole wire format for `metadata.json` written by the conversion subprocess and read by the parent process.
The model SHALL carry, at minimum: `pipeline_name`, `terminal_ref`, `content_type`, artifact filenames (`markdown_filename`, `figure_files`), pipeline/converter version (`docling_version`, `config_snapshot`), the final `source_meta: SourceMetadata` observed during conversion, the raw `document_title: str | None` returned by the converter, and the selected `source_title: str | None` chosen per the title-selection policy in the conversion-worker spec.

The subprocess SHALL serialise `SubprocessMetadata` to `metadata.json`; the parent SHALL deserialise the same type.
Both sides SHALL reject unknown fields (`extra="forbid"`-equivalent semantics).

`SubprocessMetadata.source_title` SHALL be the authoritative value for the manifest and `ConversionOutput`; downstream consumers SHALL NOT re-derive it from the Source row.

#### Scenario: Subprocess writes typed metadata

- **GIVEN** the conversion subprocess has produced artifacts and selected a `source_title`
- **WHEN** it writes `metadata.json`
- **THEN** the file contains a serialised `SubprocessMetadata` carrying `source_meta`, `document_title`, and `source_title` alongside the existing fields

#### Scenario: Parent rejects malformed subprocess metadata

- **GIVEN** the parent reads a `metadata.json` whose payload contains an unknown field or a missing required field
- **WHEN** it deserialises into `SubprocessMetadata`
- **THEN** a typed validation error is raised and the job fails non-retryably

### Requirement: Declare content fetching as a protocol with two roles

The system SHALL support two fetcher roles — content fetchers and ref resolvers — each with a distinct protocol.
A content fetcher SHALL accept a `SourceRef` and a `SourceMetadata` value, and return a `ConversionInput` containing the fetched bytes, authoritative content type, and a (possibly enriched) `SourceMetadata`.
A ref resolver SHALL accept a `SourceRef` and return a tuple of `(SourceRef, SourceMetadata)` — a more specific `SourceRef` and any source-descriptive fields it observed during resolution — deferring byte-level fetching to a downstream content fetcher.
A `SourceRef` SHALL remain a pure identity / fetch-instruction value (used for hashing, registry dispatch, and `source_ref_hash`); source-descriptive fields SHALL flow exclusively through `SourceMetadata`, not as fields on `SourceRef` variants.
All other constraints from the prior requirement (class-level `produces` / `resolves_to`, structural role detection via `@runtime_checkable`, registration-time and dispatch-time isinstance checks, no class-level submittability flag) SHALL be preserved. (Previously: content fetchers accepted only a `SourceRef`; ref resolvers returned only a `SourceRef`.
Source-descriptive metadata observed during resolution had no return path and was discarded.)

#### Scenario: Content fetcher receives and returns SourceMetadata

- **GIVEN** a `SourceRef` whose kind maps to a registered content fetcher and a `SourceMetadata` from the coordinator
- **WHEN** the fetcher is invoked
- **THEN** a `ConversionInput` is returned whose `source_meta` field carries forward all non-`None` fields supplied by the caller, plus any fields the fetcher itself observed, combined via `SourceMetadata.merge()`

#### Scenario: Ref resolver returns refined ref with metadata

- **GIVEN** a `SourceRef` whose kind maps to a registered ref resolver
- **WHEN** the resolver is invoked
- **THEN** a `(SourceRef, SourceMetadata)` tuple is returned where the ref is of a different kind and the metadata holds any source-descriptive fields the resolver observed (or all-`None` fields if none were observed)

#### Scenario: SourceRef carries no descriptive fields

- **GIVEN** a `SourceRef` variant of any kind
- **WHEN** its public field set is inspected
- **THEN** it contains only identity / fetch-instruction fields (e.g. `bookmark_id`, `url`, `arxiv_id`); descriptive fields such as a canonical display URL or a human title appear only on `SourceMetadata`

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
- **THEN** a new `SourceRef` of a different kind is returned, to be dispatched on by the coordinator

#### Scenario: Dispatch role is determined structurally at runtime

- **GIVEN** a registered adapter and a `SourceRef` whose kind maps to it
- **WHEN** the coordinator dispatches the ref
- **THEN** it invokes `impl.resolve(ref)` when `isinstance(impl, RefResolver)` is true and `impl.fetch(ref, source_meta)` otherwise

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
A converter SHALL accept a `ConversionInput` and return `ConversionArtifacts` containing the converted output, any extracted assets, and a `document_title: str | None` reflecting the document's own title when one is present in the source.
The converter SHALL return `document_title` as a raw observation and SHALL NOT apply title-selection policy (e.g. fallback to resolver title); selection across `document_title` and `source_meta.resolver_title` is owned by the conversion-worker layer.
All other constraints from the prior requirement (static `supported_formats`, static `requires_gpu`) SHALL be preserved. (Previously: `ConversionArtifacts` carried only converted output and extracted assets; document-level metadata observed during conversion had no return path and was discarded.)

#### Scenario: Converter emits raw document title when present

- **GIVEN** a `ConversionInput` whose source contains a document-level title
- **WHEN** the converter is invoked
- **THEN** the returned `ConversionArtifacts.document_title` is the document's title verbatim, with no UUID/URL filtering or fallback applied

#### Scenario: Converter emits null document title when source has none

- **GIVEN** a `ConversionInput` whose source contains no document-level title
- **WHEN** the converter is invoked
- **THEN** the returned `ConversionArtifacts.document_title` is `None` regardless of any value present in `input.source_meta.resolver_title`

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

### Requirement: Inject fetcher and converter resolution into the coordinator

The coordinator SHALL receive its fetcher resolver and converter resolver as constructor dependencies, and SHALL not import or reference any concrete adapter module.
The coordinator's result type SHALL carry the converter name and its config snapshot so that callers (e.g., the worker) do not need to re-resolve the converter to obtain those values.
Specifically, `ProcessResult` SHALL include `converter_name: str` and `config_snapshot: dict[str, Any]`, written by the coordinator before returning to the caller.
No caller SHALL access `ConversionCoordinator._resolve_converter` directly; the private resolver method is an implementation detail of the coordinator and SHALL NOT be reached from outside the coordinator class.

#### Scenario: ConversionCoordinator operates with injected fakes

- **GIVEN** a coordinator constructed with fake resolver callables returning in-memory fetchers and converters
- **WHEN** a job is processed
- **THEN** the coordinator completes the fetch-convert cycle using only the injected fakes, with no dependency on real adapters or registries

#### Scenario: ConversionCoordinator has no transitive import of adapter modules

- **GIVEN** the coordinator's module source
- **WHEN** its import graph is inspected
- **THEN** no adapter module (e.g., docling, karakeep, arxiv) appears in the transitive closure

#### Scenario: ProcessResult carries converter metadata

- **GIVEN** a coordinator that successfully completes the fetch-convert cycle
- **WHEN** the caller receives the `ProcessResult`
- **THEN** `result.converter_name` and `result.config_snapshot` are populated with the converter
  that ran and its output-affecting configuration snapshot, without the caller making any
  additional resolver calls

#### Scenario: Worker consumes config_snapshot from ProcessResult directly

- **GIVEN** a worker that receives a `ProcessResult` from the coordinator
- **WHEN** the worker constructs the idempotency key or records provenance
- **THEN** it reads `config_snapshot` from the result without calling any coordinator method
  beyond `process_with_provenance`

### Requirement: Enforce GPU admission control above the subprocess boundary

The system SHALL bound the number of GPU-consuming conversion subprocesses running concurrently via a GPU `ResourceGuard` acquired in the parent process before subprocess spawn.
The guard SHALL be a context manager implemented by a threading primitive shared across the parent's worker thread pool.
The coordinator SHALL acquire the guard if and only if the dispatched converter declares `requires_gpu == True`; converters declaring `requires_gpu == False` SHALL spawn without contending on the GPU guard.
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

The system SHALL assemble registries, adapters, resource guards, and the coordinator via role-specific builder functions in a single wiring package.
Each builder SHALL register only the adapters, probes, and accepted source kinds appropriate for its process role.
The wiring package SHALL be the only package that imports both core protocols and concrete adapter implementations.
Role-specific builder functions SHALL construct all `BaseSettings` instances exactly once and SHALL NOT allow settings to be re-read from disk or environment on subsequent calls.
Every `BaseSettings` subclass in the conversion package SHALL default `env_file=None` in its `model_config`; the composition root (builder functions) is the only permitted site that loads `.env` via `python-dotenv` before constructing settings.
Settings instances that are needed at request time (e.g., `DoclingConverterConfig`) SHALL be attached to `app.state` by the builder function and read via `request.app.state` by request handlers; they SHALL NOT be re-instantiated per request or per health probe.

#### Scenario: Worker builder registers all adapters

- **GIVEN** a worker process starting up
- **WHEN** `build_worker_runtime(cfg)` is called
- **THEN** all fetcher and converter adapters are registered, the GPU guard is created, and the coordinator is fully wired

#### Scenario: API builder provides accepted-kinds set

- **GIVEN** an API process starting up
- **WHEN** `build_api_runtime(cfg)` is called
- **THEN** the accepted source-ref kinds are derived from the fetcher registry and made available for request validation

#### Scenario: Wiring package is the sole cross-cutting import site

- **GIVEN** the project's import graph
- **WHEN** adapter modules are traced as importers
- **THEN** only the wiring package imports both core and adapter packages

#### Scenario: DoclingConverterConfig is constructed once per process

- **GIVEN** an API process that handles multiple job-submission requests
- **WHEN** each request is processed
- **THEN** the `DoclingConverterConfig` instance used for idempotency-key computation is the
  same object constructed at startup, not a new instance per request

#### Scenario: BaseSettings subclass does not read .env by default

- **GIVEN** a `DoclingConverterConfig` or `IngressPolicy` constructed with no explicit
  `_env_file` argument
- **WHEN** the instance is created
- **THEN** no `.env` file on disk is read; field values come only from environment variables
  already present in the process or from explicit constructor arguments

### Requirement: Expose a deployment capability descriptor for worker dispatch

The system SHALL expose a `DeploymentCapabilities` descriptor — produced by worker wiring — describing the worker-side capabilities of the running deployment: which `SourceRef` kinds the worker can dispatch (full `FetcherRegistry.registered_kinds()`, spanning both resolvers and content fetchers), which `ContentType` values have a registered converter, and which startup probes the registered adapters declare.
Adapters that are not yet ready to serve SHALL NOT be registered in the registry — their implementation classes may exist in the codebase as skeletons, but the composition root does not wire them until they function.
`DeploymentCapabilities` is consumed by the worker for dispatch and by observability/introspection surfaces; it is NOT the authority for public-ingress acceptability.

#### Scenario: Worker descriptor reports registered dispatch kinds

- **GIVEN** worker wiring has registered `KarakeepBookmarkResolver` and content fetchers for `"arxiv"`, `"github_readme"`, `"url"`, and `"inline_html"`
- **WHEN** `DeploymentCapabilities` is built
- **THEN** `registered_kinds` contains `"karakeep_bookmark"`, `"arxiv"`, `"github_readme"`, `"url"`, `"inline_html"` — every kind the coordinator can dispatch

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
Each variant SHALL expose a `to_dedup_payload() -> dict` method returning a canonical, normalized representation used for dedup-key hashing (see "Compute source_ref_hash from a canonical dedup payload").
Exception: `InlineHtmlRef` MAY embed content bytes directly, subject to a hard size cap of 64 KiB measured on the raw body bytes (not the serialized JSON form), as a documented exception for small inline-text payloads.
Serialized-JSON bloat from escaping is accepted; typical expansion is ~1.3× for HTML-shaped content.

Variants MAY carry cosmetic or forward-compatibility fields that are excluded from `to_dedup_payload()` and MAY also be ignored by the current fetcher implementation.
At cutover the following fields are accepted at deserialization but intentionally non-load-bearing:

- `ArxivRef.arxiv_pdf_url` — cosmetic fetcher hint preserved for observability; does not affect fetch behavior or the dedup key.
- `GithubReadmeRef.branch` — accepted for forward compatibility but currently ignored by `GithubReadmeFetcher`, which hardcodes a `main`/`master` fallback.
  Wiring branch through to the fetcher is deferred until `IngressPolicy` widens to admit `github_readme` for public submission.

Accepted-but-ignored fields SHALL be documented both on the model (docstring) and in this specification so that consumers are not misled into believing the field is load-bearing at cutover.

`KarakeepBookmarkRef.bookmark_id` SHALL satisfy the constraint `^[A-Za-z0-9_-]{1,64}$`, validated at Pydantic construction time.
Values that do not match SHALL be rejected with a validation error before any downstream processing.

`UrlRef` SHALL validate input URLs with the project's canonical URL normalizer; import failures SHALL surface at module-load time, not deferred to first use.
The `_normalize` validator SHALL catch only the documented error types of the URL normalizer (`ValueError`, `validators.ValidationError`); it SHALL NOT use bare `except Exception`.
When the normalizer raises a recognized error, the fallback SHALL apply deterministic normalization (`strip` + `casefold` + `rstrip("/")`) rather than bare `strip()`, so the fallback output is stable across environments.

#### Scenario: Accepted-but-ignored field round-trips without affecting the dedup key

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

#### Scenario: Invalid bookmark_id rejected at construction

- **GIVEN** a `KarakeepBookmarkRef` construction with a `bookmark_id` value that contains
  spaces, non-ASCII characters, or exceeds 64 characters
- **WHEN** the model is constructed
- **THEN** a pydantic `ValidationError` is raised before the ref reaches any storage or
  HTTP call-site

#### Scenario: normalize_url import failure surfaces at startup

- **GIVEN** a process where `aizk.utilities.url_utils` cannot be imported (missing dependency)
- **WHEN** the `aizk.conversion.core.source_ref` module is imported
- **THEN** the import fails immediately, not silently at the first URL submission

#### Scenario: UrlRef normalizer exception falls back deterministically

- **GIVEN** a URL string that `normalize_url` raises `ValueError` for
- **WHEN** `UrlRef` is constructed with that string
- **THEN** the stored value is `url.strip().casefold().rstrip("/")`, identical across
  environments and distinct from bare `url.strip()`

### Requirement: Compute source_ref_hash from a canonical dedup payload

The system SHALL compute `source_ref_hash` by invoking each `SourceRef` variant's `to_dedup_payload()` method to obtain a canonical, normalized dict containing only the fields that define dedup sameness for that variant, then hashing the JSON-encoded payload with stable key ordering and separators.
The hash SHALL NOT be derived from `model_dump_json()` of the full ref, so cosmetic changes (field ordering, default values, non-sameness fields) do not produce different hashes for the same logical source.

The canonical dedup payload constitutes a versioned dedup-key contract.
Any change that alters the `to_dedup_payload()` output for previously-accepted refs (key rename, normalization rule change, field added to or removed from the payload) SHALL be treated as a breaking change to the Source **dedup key** (`source_ref_hash`) — the content fingerprint that determines which submissions resolve to the same `source_id`, not the durable identity itself — and SHALL be accompanied by a data migration that recomputes `source_ref_hash` for affected rows.
Additive `SourceRef` fields that do not participate in `to_dedup_payload()` are not breaking.
A fixture-lock test SHALL pin a curated set of `(variant_instance, expected_sha256)` pairs so accidental drift in `to_dedup_payload()` output (e.g., whitespace, key ordering, normalization) fails CI before shipping.

All `SourceRef` variants' `to_dedup_payload()` implementations SHALL apply consistent normalization to string sameness-key fields before including them in the payload:

- String fields used in the sameness key SHALL have leading/trailing whitespace stripped.
- Fields on case-insensitive external namespaces SHALL be casefolded.
  Specifically: `GithubReadmeRef.owner` and `GithubReadmeRef.repo` SHALL be lowercased (GitHub org/repo names are case-insensitive); `KarakeepBookmarkRef.bookmark_id` SHALL have whitespace stripped (pattern constraint prevents embedded whitespace; strip guards against edge cases at the boundary).

A fixture-lock test SHALL pin one normalization-sensitive instance per variant (e.g., a `GithubReadmeRef` with mixed-case `owner`) to confirm that casefolding is applied before hashing.

#### Scenario: Dedup payload fixture-lock guards against accidental drift

- **GIVEN** a curated fixture of `SourceRef` instances with their expected `source_ref_hash` values (one per variant, each including at least one non-trivial normalization case)
- **WHEN** `compute_source_ref_hash` is run against each fixture instance
- **THEN** the computed hash equals the pinned expected hash; a change to `to_dedup_payload()` that alters any output fails this test and signals a breaking dedup-key change requiring a migration

#### Scenario: Equivalent refs produce identical hash

- **GIVEN** two `ArxivRef` instances with the same `arxiv_id` but differing cosmetic fields (e.g., `arxiv_pdf_url` present vs. absent)
- **WHEN** `source_ref_hash` is computed for each
- **THEN** the hashes are identical

#### Scenario: Sameness-defining field differs

- **GIVEN** two `ArxivRef` instances with different `arxiv_id` values
- **WHEN** `source_ref_hash` is computed for each
- **THEN** the hashes differ

#### Scenario: InlineHtmlRef hash is content-addressed

- **GIVEN** two `InlineHtmlRef` instances with identical body bytes
- **WHEN** `source_ref_hash` is computed for each
- **THEN** the hashes are identical (payload hashes the body content, not metadata)

#### Scenario: Mixed-case GitHub owner/repo produces the same hash as lowercased

- **GIVEN** `GithubReadmeRef(owner="MyOrg", repo="MyRepo")` and
  `GithubReadmeRef(owner="myorg", repo="myrepo")`
- **WHEN** `compute_source_ref_hash` is run against each
- **THEN** the hashes are identical

#### Scenario: Bookmark ID with leading/trailing whitespace normalizes to same hash

- **GIVEN** `KarakeepBookmarkRef(bookmark_id="abc123")` and an attempt to construct
  `KarakeepBookmarkRef(bookmark_id=" abc123")` (leading space)
- **WHEN** the second construction is attempted
- **THEN** a pydantic `ValidationError` is raised (pattern forbids whitespace), so two
  callers cannot produce divergent hashes for the logically same bookmark

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

### Requirement: Apply network egress policy to all external-content dereferences

Any pipeline component (fetcher, ref resolver, converter) that dereferences a URL or local filesystem path derived from external content SHALL apply the egress policy defined below before issuing the request or read.
A "dereference of external content" is any operation that resolves a URL to its bytes, opens a filesystem path, or causes another component to do so on its behalf (e.g., a converter passing fetched HTML into a backend that itself follows `<img src>` references).

The egress policy is **deny-list-only** with a fixed deny set:

- IPv4: loopback (`127.0.0.0/8`), private ranges (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`), shared-address space (`100.64.0.0/10`, RFC 6598), link-local (`169.254.0.0/16`, including the cloud-metadata address `169.254.169.254`), unspecified-source (`0.0.0.0/8`), broadcast (`255.255.255.255`), and IPv4 multicast (`224.0.0.0/4`).
- IPv6: loopback (`::1/128`), unspecified (`::/128`), unique local (`fc00::/7`), link-local (`fe80::/10`), multicast (`ff00::/8`), and the AWS IPv6 metadata address `fd00:ec2::254`.
- Schemes: any scheme outside `https` and `http`.

Network dereferences SHALL:

- Resolve the destination hostname to one or more IP addresses, reject the request if any resolved address is in the deny set, and connect to a validated address with the original Host header preserved (so DNS-rebinding cannot swap the connected IP after validation).
- Re-apply the policy at every redirect hop; redirect chains SHALL NOT cross a hop whose target fails validation, regardless of how many hops preceded.
- Reject schemes outside `https` and `http`.

**A location named by external content SHALL NOT be dereferenced as a local filesystem path at any point in the pipeline.**
This prohibition is on locations the external content names.
Paths the pipeline itself composes for its own artifacts — workspace files, figure outputs, manifest entries — are not locations named by external content, and remain governed by the workspace-containment rules that apply to artifact paths.

**The converter SHALL be configured such that it performs no dereference of any location it finds in the content it is given**, whether that location appears in a reference shape the pipeline recognizes or in any other.
For any resource an external document references, admission is therefore the pipeline's decision and not the converter's: the bytes of an admitted resource SHALL be supplied to the converter by the pipeline, and no reference the pipeline has not admitted SHALL be resolvable by the converter.
A reference that carries its own bytes rather than naming a location SHALL be admitted without any fetch or read.

Every non-admission SHALL be recorded individually, identifying the reference and the class of policy violation or resource cap that caused it, and the per-conversion record of the phase SHALL account for every non-admitted reference.

A **rejection** is a non-admission caused by an egress-policy violation.
Rejections SHALL surface as a typed error and SHALL be classified as non-retryable; the failure SHALL identify the policy violation class (deny-list category, scheme, redirect hop, or out-of-workspace artifact path) without echoing the rejected destination back into structured output served to clients.
A rejection raised while dereferencing a source SHALL fail the job.
A rejection raised while admitting a resource that already-fetched content references SHALL drop that reference without failing the job.
A non-admission caused by a resource cap rather than a policy violation SHALL likewise drop the reference without failing the job, and SHALL NOT be required to surface as a typed error.

**Operator-trusted KaraKeep carve-out — parsed-origin exact match, never string prefix.**
A candidate URL qualifies for the KaraKeep trusted-infrastructure path only when its effective origin exactly matches the configured `karakeep_base_url` origin (`scheme`, normalized hostname, and effective port) **and** its path begins with `/api/v1/assets/`.
URLs that merely share a textual prefix with `karakeep_base_url` SHALL NOT qualify.
Same-origin URLs outside the asset path prefix SHALL NOT qualify.
Non-qualifying URLs SHALL be treated as ordinary outbound URLs and SHALL go through the normal egress-validation path.

**Trust boundary — fetch time, not construction time.**
`UrlRef` construction performs URL normalization for dedup identity but does **not** call the egress validator and does **not** resolve DNS.
The load-bearing trust boundary is fetch time: every fetcher (`UrlFetcher`, `ArxivFetcher`, `GithubReadmeFetcher`) and the image-prefetch path route through `egress_fetch_bytes`, which calls the async egress validator for the initial hop and again for every redirect target.
A `UrlRef` instance therefore _can_ exist in the system carrying a deny-set URL — the security property is that no outbound socket connection ever opens to a deny-set destination, not that the model rejects the URL at construction.
This shape exists because (1) operator-configured KaraKeep base URLs are typically on a private network and asset fetches that target them are routed through `KarakeepClient` (operator-trusted infrastructure, intentionally exempt from the egress gate), and (2) construction-time DNS would otherwise run synchronously in API request handling, in worker rehydration paths, and inside every resolver-built `UrlRef` — duplicating DNS load and creating a queryable resolution-latency oracle on the public submission endpoint.

#### Scenario: Cloud-metadata IPv4 destination rejected

- **GIVEN** a fetcher is invoked on a `SourceRef` whose target host resolves to `169.254.169.254`
- **WHEN** the fetcher attempts the dereference
- **THEN** a typed egress-policy error is raised before any network request is issued, the job is classified as a non-retryable failure, and no metadata-service response reaches the converter

#### Scenario: Cloud-metadata IPv6 destination rejected

- **GIVEN** a fetcher is invoked on a `SourceRef` whose target host resolves to `fd00:ec2::254`
- **WHEN** the fetcher attempts the dereference
- **THEN** a typed egress-policy error is raised before any network request is issued

#### Scenario: RFC1918 destination rejected

- **GIVEN** a fetcher is invoked on a `SourceRef` whose target host resolves to an address in `10.0.0.0/8`, `172.16.0.0/12`, or `192.168.0.0/16`
- **WHEN** the fetcher attempts the dereference
- **THEN** a typed egress-policy error is raised before any network request is issued

#### Scenario: Loopback destination rejected

- **GIVEN** a fetcher is invoked on a `SourceRef` whose target host resolves to `127.0.0.1` or `::1`
- **WHEN** the fetcher attempts the dereference
- **THEN** a typed egress-policy error is raised

#### Scenario: Public host redirecting to private IP rejected at the redirect hop

- **GIVEN** a fetcher is invoked on a `SourceRef` whose initial host resolves to a public address but the response is a 3xx redirect to a URL whose host resolves into the deny set
- **WHEN** the fetcher follows the redirect
- **THEN** the egress policy is re-evaluated, the redirect hop is rejected with a typed error, and the redirect target is never fetched

#### Scenario: DNS rebinding cannot swap the connected IP after validation

- **GIVEN** a fetcher whose pre-connection DNS resolution returns a public IP for the target host
- **WHEN** the underlying socket connection is established
- **THEN** the connection uses the validated IP captured at validation time (not a fresh resolution), so a same-name later-resolution returning a private IP cannot reach the connection layer

#### Scenario: Non-http(s) scheme rejected

- **GIVEN** a `SourceRef` whose URL uses a scheme other than `http` or `https` (e.g., `file://`, `data:`, `javascript:`, `gopher://`, `ftp://`)
- **WHEN** the fetcher attempts the dereference
- **THEN** a typed egress-policy error is raised before any I/O is performed

#### Scenario: Converter outbound HTTP fetch routed through the egress gate

- **GIVEN** an HTML document containing `<img src="http://169.254.169.254/...">` is handed to the conversion pipeline
- **WHEN** the document is prepared for the converter
- **THEN** the image-fetch attempt passes through the same egress validation as a `SourceRef` dereference, the cloud-metadata target is rejected before any network request is issued, the reference is not resolvable by the converter, the rejection is recorded with its policy violation class, and the job completes rather than failing

#### Scenario: Reference in an unrecognized shape is not resolvable by the converter

- **GIVEN** an HTML document that names a resource in a shape the pipeline's admission step does not recognize, such as `<source srcset="http://169.254.169.254/x.png">` or a CSS `url()` declaration
- **WHEN** the document is converted
- **THEN** the converter's configuration leaves the reference unresolvable, so no network request and no filesystem read is performed for it

#### Scenario: Local-path image reference never dereferenced

- **GIVEN** an HTML document containing `<img src="/etc/ssh/ssh_host_rsa_key">` is handed to the conversion pipeline
- **WHEN** the document is prepared for the converter
- **THEN** the reference is not admitted, it is not resolvable by the converter, its refusal is recorded, and no read of `/etc/ssh/ssh_host_rsa_key` is performed

#### Scenario: Traversal image reference never dereferenced

- **GIVEN** an HTML document containing `<img src="../../../../etc/passwd">` is handed to the conversion pipeline
- **WHEN** the document is prepared for the converter
- **THEN** the reference is not admitted, it is not resolvable by the converter, its refusal is recorded, and no read of `/etc/passwd` is performed

#### Scenario: Egress-admitted image reaches the converted output

- **GIVEN** an HTML document whose `<img src>` names a resource that passes egress validation and is within the per-document caps
- **WHEN** the document is converted
- **THEN** the resource's bytes appear as a figure in the converted document, and the converter performs no local filesystem read derived from the document

#### Scenario: Relative image reference resolved against the source URL

- **GIVEN** an HTML document fetched from a known source URL containing `<img src="images/photo.png">`
- **WHEN** the document is prepared for the converter
- **THEN** the reference is resolved against the source URL and evaluated by the egress policy as an ordinary outbound URL

#### Scenario: Inline data reference admitted without any dereference

- **GIVEN** an HTML document containing an `<img>` whose `src` is a `data:` URI
- **WHEN** the document is prepared for the converter
- **THEN** the reference is admitted unchanged, and no network request and no filesystem read is performed for it

#### Scenario: Cap-exceeded image reference dropped rather than failing the job

- **GIVEN** an HTML document whose image references exceed a per-document pre-fetch cap
- **WHEN** the document is prepared for the converter
- **THEN** each reference beyond the cap is not resolvable by the converter, its refusal is recorded with the cap that caused it, and the job does not fail

#### Scenario: UrlRef construction does not perform egress validation

- **GIVEN** a JSON value `{"kind": "url", "url": "http://169.254.169.254/latest/meta-data/"}`
- **WHEN** `UrlRef` is constructed via pydantic deserialization
- **THEN** construction succeeds (no DNS lookup, no destination classification); the URL is normalized for dedup identity; the deny-set rejection happens at fetch time when the fetcher dispatches

#### Scenario: KaraKeep resolver emits a UrlRef whose URL targets a private host (operator-trusted base URL)

- **GIVEN** `KarakeepBookmarkResolver` processes a bookmark whose Step 3 or Step 4 path constructs `UrlRef(url=f"{karakeep_base_url}/api/v1/assets/...")` and `karakeep_base_url` is on a private network
- **WHEN** the resolver returns the `UrlRef`
- **THEN** construction succeeds; the downstream `UrlFetcher` routes the request through `KarakeepClient` (the configured trusted infrastructure) and does **not** invoke `egress_fetch_bytes`; the egress deny-list is correctly inert for this path

#### Scenario: Deny-set URL rejected at fetch time

- **GIVEN** a `UrlRef(url="http://169.254.169.254/latest/meta-data/")` reaches `UrlFetcher.fetch` (e.g., constructed by a future widening of `_API_SUBMITTABLE_KINDS`)
- **WHEN** the fetcher dispatches via `egress_fetch_bytes`
- **THEN** `async_assert_egress_allowed` raises `DenyListDestination`, the fetcher propagates the typed error unwrapped, the job is classified non-retryable, and no socket connection to the metadata service is opened

#### Scenario: Exact-origin KaraKeep asset URL is trusted

- **GIVEN** `karakeep_base_url = "https://karakeep.example.internal"`
- **AND** a candidate URL of `https://karakeep.example.internal/api/v1/assets/abc123`
- **WHEN** the fetcher decides whether to use the KaraKeep trusted-infrastructure path
- **THEN** the URL qualifies for the carve-out because the origin matches exactly and the path is under `/api/v1/assets/`

#### Scenario: Lookalike host does not qualify for KaraKeep trust

- **GIVEN** `karakeep_base_url = "https://karakeep.example.internal"`
- **AND** a candidate URL of `https://karakeep.example.internal.evil.test/api/v1/assets/abc123`
- **WHEN** the fetcher decides whether to use the KaraKeep trusted-infrastructure path
- **THEN** the URL does not qualify for the carve-out and is processed through the normal egress-validation path

#### Scenario: Same-origin non-asset URL does not qualify for KaraKeep trust

- **GIVEN** `karakeep_base_url = "https://karakeep.example.internal"`
- **AND** a candidate URL of `https://karakeep.example.internal/api/v1/bookmarks/abc123`
- **WHEN** the fetcher decides whether to use the KaraKeep trusted-infrastructure path
- **THEN** the URL does not qualify for the carve-out because the path is outside `/api/v1/assets/`, and it is processed through the normal egress-validation path


### Requirement: Preserve non-retryable fetch-error classification through fetcher dispatch

A fetcher that delegates to lower-level helpers (the egress helper, the KaraKeep client, the arXiv export API) SHALL propagate any typed error whose declared retry classification is non-retryable without re-wrapping it as a retryable error.

This applies to at least:

- `EgressPolicyError` and its subclasses (already covered by the egress-policy requirement; restated here so the broader rule is explicit)
- `FetchTooLargeError`, raised by the egress helper when a response body exceeds the configured byte cap

A fetcher MAY still wrap genuinely transient lower-level exceptions (timeouts, connection resets, generic `httpx` errors) as a retryable fetch-error subclass; the prohibition is specifically on collapsing a non-retryable signal into a retryable one.

The arXiv PDF fetch path SHALL honor this rule.
A `FetchTooLargeError` raised by the egress helper SHALL surface to the job runner as `FetchTooLargeError`, not as `ArxivPdfFetchError`.

This requirement closes a classification hole on the arXiv path where oversized PDF responses were being re-raised as a retryable error, causing the worker to retry the same oversized fetch indefinitely instead of failing the job permanently.

#### Scenario: Oversized arXiv PDF surfaces as FetchTooLargeError

- **GIVEN** a fetcher invocation that resolves to an arXiv PDF whose response body exceeds `fetch_max_response_bytes`
- **WHEN** the egress helper raises `FetchTooLargeError`
- **THEN** the arXiv fetch path re-raises `FetchTooLargeError` unchanged; the error is NOT wrapped as `ArxivPdfFetchError`, and the job runner sees `retryable = False`

#### Scenario: Egress-policy rejection on arXiv path stays non-retryable

- **GIVEN** a fetcher invocation that resolves to an arXiv URL whose destination is in the egress deny set
- **WHEN** the egress helper raises an `EgressPolicyError` subclass
- **THEN** the arXiv fetch path re-raises the egress error unchanged (existing behavior; restated to pin the broader rule)

#### Scenario: Generic transient failure on arXiv path stays retryable

- **GIVEN** a fetcher invocation that resolves to an arXiv URL where the egress helper raises a generic transient exception (e.g., a connection reset or unexpected HTTP error) that carries no non-retryable classification
- **WHEN** the arXiv fetch path catches the exception
- **THEN** the path MAY wrap it as `ArxivPdfFetchError` (retryable), preserving the existing transient-failure behavior

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
