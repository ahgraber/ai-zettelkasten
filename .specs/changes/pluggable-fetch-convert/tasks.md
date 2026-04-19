# Tasks: pluggable-fetch-convert

## Stage 1 — Core protocols, types, registries, SourceRef union (additive, non-breaking)

- [x] Create `aizk/conversion/core/__init__.py` package
- [x] Create `aizk/conversion/core/types.py`: `ContentType` enum (`pdf`, `html`, `image`, `docx`, `pptx`, `xlsx`, `csv`), `ConversionInput` (bytes + content_type + metadata), `ConversionArtifacts` (markdown + figures + metadata), and `SOURCE_TYPE_BY_KIND: Mapping[str, str]` — canonical mapping from `SourceRef.kind` to the `source_type` classification stored on the Source row (`arxiv` → `"arxiv"`, `github_readme` → `"github"`, `url` / `karakeep_bookmark` / `inline_html` / `singlefile` → `"other"`)
- [x] Create `aizk/conversion/core/source_ref.py`: `SourceRef` pydantic discriminated union with variants `KarakeepBookmarkRef`, `ArxivRef`, `GithubReadmeRef`, `UrlRef`, `SingleFileRef`, `InlineHtmlRef`; 64 KiB size cap on `InlineHtmlRef` enforced against the raw body bytes (not the serialized JSON)
- [x] Implement `to_dedup_payload() -> dict` on each `SourceRef` variant — returns a canonical, normalized dict of identity-defining fields only (e.g., `KarakeepBookmarkRef → {"kind": "karakeep_bookmark", "bookmark_id": ...}`, `ArxivRef → {"kind": "arxiv", "arxiv_id": <normalized>}`, `UrlRef → {"kind": "url", "url": <normalized>}`, `InlineHtmlRef → {"kind": "inline_html", "content_hash": sha256(body)}`)
- [x] Add `compute_source_ref_hash(ref)` helper: SHA-256 of `json.dumps(ref.to_dedup_payload(), sort_keys=True, separators=(",", ":"))`
- [x] Create `aizk/conversion/core/protocols.py`: `ContentFetcher` protocol (`fetch(ref) -> ConversionInput`, `produces: ClassVar[frozenset[ContentType]]`), `RefResolver` protocol (`resolve(ref) -> SourceRef`, `resolves_to: ClassVar[frozenset[str]]` class-level attribute enumerating every kind the resolver may emit), `Converter` protocol (`supported_formats: frozenset[ContentType]`, `requires_gpu: bool` class-level attribute, `convert(input) -> ConversionArtifacts`, `config_snapshot() -> dict`), `ResourceGuard` protocol (context manager; acquiring thread is sole releaser); all fetcher/resolver protocols are `@runtime_checkable`.
  Fetcher protocols SHALL NOT declare any `api_submittable` / public-ingress flag — public-ingress acceptability is a deployment policy (see PR 5 `IngressPolicy`), not an adapter attribute.
- [x] Create `aizk/conversion/core/registry.py`: `FetcherRegistry` with distinct registration entry points `register_content_fetcher(kind, impl)` and `register_resolver(kind, impl)` — role is declared at registration, AND each entry point validates structural conformance (`register_content_fetcher` raises `RegistrationRoleMismatch` if `isinstance(impl, RefResolver)` or if `impl` does not satisfy `ContentFetcher`; `register_resolver` raises `RegistrationRoleMismatch` if `impl` does not satisfy `RefResolver`); kind uniqueness enforced across both roles; `registered_kinds() -> frozenset[str]` returns the union of registered kinds (used for worker dispatch and chain-closure validation); raises `FetcherNotRegistered` on unknown kind; `ConverterRegistry` (maps `(content_type, impl_name)` -> converter, raises `NoConverterForFormat`).
  Registry exposes NO `submittable_kinds()` method — public-ingress acceptability is not a registry concern.
- [x] Create `aizk/conversion/core/errors.py`: `FetcherNotRegistered`, `NoConverterForFormat`, `FetcherDepthExceeded`, `ChainNotTerminated`, `RegistrationRoleMismatch` typed errors with retryability classification (`ChainNotTerminated` and `RegistrationRoleMismatch` are startup-time errors, not retryable)
- [x] Tests: `SourceRef` JSON round-trip for each variant; unknown kind rejected on deserialization; `InlineHtmlRef` rejected when raw body exceeds 64 KiB, accepted when raw body fits even if JSON-escaped form is larger
- [x] Tests: `to_dedup_payload` — equivalent refs with cosmetic differences produce identical hashes; identity-field changes produce different hashes; `InlineHtmlRef` hash is content-addressed
- [x] Tests: `FetcherRegistry` — `register_content_fetcher` and `register_resolver` register the correct role; duplicate kind across roles is rejected; unregistered kind raises `FetcherNotRegistered`; `registered_kinds()` returns the union of both roles
- [x] Tests: `FetcherRegistry` role-mismatch rejection — `register_content_fetcher` raises `RegistrationRoleMismatch` when given an impl that satisfies `RefResolver`; `register_resolver` raises `RegistrationRoleMismatch` when given an impl that does not satisfy `RefResolver`; registry state is unchanged in both rejection cases
- [x] Tests: `Converter.requires_gpu` is a class-level boolean inspectable without instantiation
- [x] Tests: `RefResolver.resolves_to` is a class-level `frozenset[str]` inspectable without instantiation
- [x] Tests: `ConverterRegistry` — register multi-format converter, resolve by `(content_type, name)`, missing combo error
- [x] Tests: `ContentType` enum has all 7 members
- [x] Tests: `SOURCE_TYPE_BY_KIND` has an entry for every `SourceRef` variant's `kind` literal (fail-fast on adding a new variant without classifying it)
- [x] Tests: dedup payload fixture-lock — a pinned fixture of `(SourceRef instance, expected SHA-256)` pairs (covering every variant and at least one non-trivial normalization case per variant, e.g., ArxivRef with and without `arxiv_pdf_url`, UrlRef before and after normalization) — `compute_source_ref_hash` output equals the pinned expected hash for every fixture entry; this test is intended to fail loudly on any accidental change to `to_dedup_payload()` output so the author must either revert the change or ship a data migration

## Stage 2 — Orchestrator class with fakes-based tests (additive, non-breaking)

- [x] Create `aizk/conversion/core/orchestrator.py`: `Orchestrator.__init__(resolve_fetcher, resolve_converter)` with DI callables; `_fetch(ref, depth)` with recursive dispatch and depth cap (default 2); `process(ref, converter_name) -> ConversionArtifacts`
- [x] Tests: orchestrator with fake content fetcher — single-hop fetch returns `ConversionInput`
- [x] Tests: orchestrator with fake ref resolver + content fetcher — two-hop resolution succeeds
- [x] Tests: depth limit exceeded raises `FetcherDepthExceeded`; error message includes the configured cap, the sequence of kinds traversed, and the configuration key used to raise the cap (so operators know how to respond when the default is legitimately too low)
- [x] Tests: orchestrator has no transitive import of any adapter module (inspect import graph)
- [x] Tests: orchestrator constructed with injected fakes completes fetch-convert cycle with no dependency on real adapters

## Stage 3 — Docling adapter extraction (move + re-export, non-breaking)

- [x] Inventory existing conversion tests: enumerate every test module that imports `converter.py`, `fetcher.py`, `bookmark_utils.py`, `arxiv_utils.py`, `github_utils.py`, or the orchestrator.
  Classify each as (stays at current location, moves alongside its target adapter, or rewrites post-cutover in PR 7).
  Commit the inventory as a checklist in the change directory so PRs 3-7 can track progress against it
- [x] Create `aizk/conversion/adapters/__init__.py` and `aizk/conversion/adapters/converters/__init__.py`
- [x] Create `aizk/conversion/adapters/converters/docling.py`: extract `DoclingConverter` from existing `converter.py`; implement `Converter` protocol with `supported_formats = frozenset({ContentType.PDF, ContentType.HTML})` and `requires_gpu = True`; supply `config_snapshot()` returning same output-affecting fields as today
- [x] Add re-export from old `converter.py` module path to avoid breaking internal imports
- [x] Tests: `DoclingConverter.supported_formats` contains `PDF` and `HTML`; `DoclingConverter.requires_gpu == True`
- [x] Tests: `DoclingConverter.config_snapshot()` returns the same field set as today's Docling-specific config hash
- [x] Tests: existing converter tests continue to pass (import path compatibility)

## Stage 4 — Fetcher adapter extraction (move + re-export, non-breaking)

- [x] Create `aizk/conversion/adapters/fetchers/__init__.py`
- [x] Create `aizk/conversion/adapters/fetchers/karakeep.py`: extract `KarakeepBookmarkResolver` (implements `RefResolver`); preserve exact 7-step resolution precedence from `orchestrator.py:194-224`; return `ArxivRef`, `GithubReadmeRef`, `UrlRef`, or `InlineHtmlRef` as appropriate; declare `resolves_to = frozenset({"arxiv", "github_readme", "url", "inline_html"})`
- [x] Create `aizk/conversion/adapters/fetchers/arxiv.py`: extract `ArxivFetcher` (implements `ContentFetcher`); preserve 3-step PDF source precedence (KaraKeep asset → `arxiv_pdf_url` → abstract page resolution)
- [x] Create `aizk/conversion/adapters/fetchers/github.py`: extract `GithubReadmeFetcher` (implements `ContentFetcher`)
- [x] Create `aizk/conversion/adapters/fetchers/url.py`: extract `UrlFetcher` (implements `ContentFetcher`)
- [x] Create `aizk/conversion/adapters/fetchers/singlefile.py`: `SingleFileFetcher` skeleton class (implements `ContentFetcher`, raises `NotImplementedError`).
  Do NOT register it in the shared registration helper — the class exists for future work but is not wired into the registry until implemented, so `"singlefile"` does not appear in `FetcherRegistry.registered_kinds()`
- [x] Create `aizk/conversion/adapters/fetchers/inline.py`: `InlineContentFetcher` (implements `ContentFetcher`); returns embedded bytes from `InlineHtmlRef` as `ConversionInput`
- [x] Add re-exports from old module paths (`fetcher.py`, `bookmark_utils.py`, `arxiv_utils.py`, `github_utils.py`)
- [x] Tests: `KarakeepBookmarkResolver` resolution precedence — arxiv bookmark returns `ArxivRef`, github returns `GithubReadmeRef`, PDF asset returns `UrlRef`, HTML content returns `UrlRef`, text-only returns `InlineHtmlRef`, empty returns error
- [x] Tests: `ArxivFetcher` PDF source precedence — asset preferred, `arxiv_pdf_url` fallback, abstract page resolution
- [x] Tests: `InlineContentFetcher` returns embedded bytes as `ConversionInput` with `ContentType.HTML`
- [x] Tests: existing fetcher/utils tests continue to pass (import path compatibility)

## Stage 5 — Wiring package with role-specific builders (additive, non-breaking)

- [x] Create `aizk/conversion/wiring/__init__.py`
- [x] Create `aizk/conversion/wiring/capabilities.py`: two descriptors.
  `DeploymentCapabilities` (worker-side) with `registered_kinds: frozenset[str]` (sourced from `FetcherRegistry.registered_kinds()`), `content_types_for(kind) -> frozenset[ContentType]`, `converter_available(content_type) -> bool`, `startup_probes: list[Probe]`.
  `SubmissionCapabilities` (API-side) with `accepted_submission_kinds: frozenset[str]` (sourced from `IngressPolicy.accepted_submission_kinds` in config).
  No `is_ready(kind)`, `resolver_chain_terminates(kind)`, or `submittable_kinds()` concepts.
- [x] Create `aizk/conversion/wiring/ingress_policy.py` (or add to config models): `IngressPolicy` config type with `accepted_submission_kinds: frozenset[str]`, populated from config (e.g., `AIZK_INGRESS__ACCEPTED_SUBMISSION_KINDS` or the equivalent pydantic config field).
  Default value at cutover: `frozenset({"karakeep_bookmark"})`.
- [x] Create `aizk/conversion/wiring/registrations.py` (or similar): `register_ready_adapters(fetcher_registry, converter_registry, cfg)` — the single source of truth for which adapters are wired into the `FetcherRegistry`.
  Called by both worker and API wiring so the registered dispatch kinds cannot drift (but the API's publicly accepted subset is independent — see `IngressPolicy`).
  Skeleton adapters (e.g., `SingleFileFetcher`) are NOT called from this helper.
  After all registrations complete, the helper SHALL invoke `validate_chain_closure(fetcher_registry, depth_cap)` before returning; the check walks each resolver's `resolves_to` edges against `FetcherRegistry.registered_kinds()`, asserts every produced kind is registered, and asserts the declared DAG has no cycles and no declared path exceeds the depth cap.
  On violation, raise `ChainNotTerminated` naming the resolver and missing kind (or the cycle); process startup fails before requests are accepted.
- [x] Create `aizk/conversion/wiring/worker.py`: `build_worker_runtime(cfg)` — calls `register_ready_adapters`, creates GPU `ResourceGuard` (wrapping `threading.Semaphore`), wires and returns `Orchestrator` + guard + `DeploymentCapabilities`
- [x] Create `aizk/conversion/wiring/api.py`: `build_api_runtime(cfg)` — calls `register_ready_adapters` against its own registry instance; reads `IngressPolicy` from `cfg`; validates `accepted_submission_kinds ⊆ registered_kinds()` and raises a typed configuration error at startup if the policy references a kind not registered; returns `SubmissionCapabilities` (NOT `DeploymentCapabilities`) for request validation
- [x] Create `aizk/conversion/wiring/testing.py`: `build_test_runtime(cfg)` — fake resolvers, in-memory registries, test-configurable registrations and test-configurable `IngressPolicy`
- [x] Verify: wiring package is the only package that imports both `core` and `adapters`
- [x] Tests: `build_worker_runtime` registers all expected fetcher kinds and converter formats; `DeploymentCapabilities.registered_kinds` contains every registered kind
- [x] Tests: `build_api_runtime` returns `SubmissionCapabilities` whose `accepted_submission_kinds == IngressPolicy.accepted_submission_kinds` from config (at cutover defaults: `{"karakeep_bookmark"}`)
- [x] Tests: worker and API intentionally diverge — for the default wiring (KaraKeep resolver + four terminal content fetchers registered; `IngressPolicy.accepted_submission_kinds = {"karakeep_bookmark"}`), `DeploymentCapabilities.registered_kinds` is the full five-kind set while `SubmissionCapabilities.accepted_submission_kinds` is `{"karakeep_bookmark"}`; the two are not equal
- [x] Tests: `accepted_submission_kinds ⊆ registered_kinds` invariant — `build_api_runtime` raises a typed configuration error at startup when `IngressPolicy.accepted_submission_kinds` contains a kind that `register_ready_adapters` did not register (fixture: policy contains `"singlefile"` but the helper does not wire `SingleFileFetcher`)
- [x] Tests: `"singlefile"` is not in `registered_kinds` because `register_ready_adapters` does not wire it (skeleton class is not invoked)
- [x] Tests: `validate_chain_closure` operates on `registered_kinds` (not on `accepted_submission_kinds`) — closure check passes when every `resolves_to` target is registered, regardless of whether those targets are publicly submittable
- [x] Tests: `validate_chain_closure` passes for the default wiring (KaraKeep resolver plus content fetchers for all four produced kinds)
- [x] Tests: `validate_chain_closure` raises `ChainNotTerminated` when a resolver declares a `resolves_to` kind that is not registered (fixture drops one downstream fetcher)
- [x] Tests: `validate_chain_closure` raises `ChainNotTerminated` when two resolvers declare a cycle in their `resolves_to` sets
- [x] Tests: `validate_chain_closure` raises `ChainNotTerminated` when the declared resolver DAG has a path longer than the depth cap
- [x] Tests: import graph lint — (a) no adapter module imports any other adapter module (enforces the cross-adapter invariant from design risks); (b) no module outside `aizk/conversion/wiring/` imports both `aizk.conversion.core` and `aizk.conversion.adapters`

## Stage 6a — BREAKING (schema): Bookmark → Source generalization + Alembic migration

- [ ] Rename `datamodel/bookmark.py` → `datamodel/source.py`; rename class `Bookmark` → `Source`
- [ ] Make `karakeep_id` nullable on `Source`
- [ ] Add `source_ref` (JSON column) and `source_ref_hash` (unique indexed text column) to `Source`
- [ ] Add `source_ref` (JSON column) to `ConversionJob`
- [ ] Update `ConversionJob` FK from `bookmarks.aizk_uuid` → `sources.aizk_uuid`
- [ ] Update all internal references from `Bookmark` → `Source` (imports, type hints, variable names) across datamodel, repository, worker read-paths; leave API-layer changes to PR 6b
- [ ] Write Alembic migration: rename `bookmarks` table → `sources`, add columns, backfill existing rows with `KarakeepBookmarkRef` and computed `source_ref_hash` (via `to_dedup_payload`)
- [ ] Migration also recomputes historical `idempotency_key` values in place: for each existing job row, read its backfilled `source_ref_hash` from the Source it references, assume `converter_name = "docling"`, substitute today's Docling-specific config snapshot as the converter-supplied snapshot input, and overwrite the `idempotency_key` column with the new-formula hash; this keeps replay-idempotency intact across the cutover
- [ ] Write Alembic `downgrade`: rename `sources` → `bookmarks`, drop `source_ref` / `source_ref_hash`.
  Downgrade SHALL abort with a clear error if any row has `karakeep_id IS NULL` (non-KaraKeep source), because the pre-migration schema cannot represent it.
  Document this as "downgrade is safe only before non-KaraKeep submissions land" in the migration's docstring
- [ ] Backfill is idempotent: the backfill step SHALL skip rows whose `source_ref_hash` is already populated and skip `idempotency_key` recomputation for rows already under the new formula, so re-running the migration (or resuming after a partial failure) does not error or duplicate work
- [ ] Backfill asserts zero hash collisions: the backfill SHALL count distinct `source_ref_hash` values and fail loudly if the count is less than the row count (invariant: `KarakeepBookmarkRef → {bookmark_id}` is collision-free by construction of unique `karakeep_id`; a collision would signal upstream data corruption)
- [ ] Tests: downgrade migration round-trips cleanly on a pre-migration fixture (no non-KaraKeep rows); downgrade aborts with the documented error when a row has null `karakeep_id`
- [ ] Tests: backfill is idempotent — running the upgrade twice leaves the same state; running after a simulated mid-backfill failure resumes cleanly
- [ ] Tests: backfill's collision assertion fires on an injected fixture with two rows crafted to collide (manually constructed duplicate `karakeep_id`, which would itself be a data-integrity bug)
- [ ] Tests: migration backfill — existing bookmark rows have valid `source_ref` and `source_ref_hash`
- [ ] Tests: `idempotency_key` recomputation — a pre-refactor job row whose legacy key was produced by the old formula is, after migration, equal to the new-formula hash computed from the row's backfilled `source_ref_hash` + `"docling"` + today's Docling config snapshot
- [ ] Tests: Source identity columns (`aizk_uuid`, `source_ref`, `source_ref_hash`, `karakeep_id`) are immutable after creation at the ORM/repository layer
- [ ] Generate `schemas/after/` DB snapshot (table schema); verify the DB-schema portion of the diff matches `schemas/expected.md`
- [ ] Docs alignment: grep the repo for references to `Bookmark` / `bookmarks` / `bookmark_id` in source comments, module docstrings, `CLAUDE.md`, architecture docs, and README; update language that refers to the old name to either describe the Source generalization or, where historically accurate, clarify that the scope has widened.
  Commit the doc touch-ups in the same PR so the schema change and its documentation land together

## Stage 6b — BREAKING (API + manifest): JobSubmission/JobResponse cutover + manifest v2.0

- [ ] Create `IngressSourceRef` discriminated union in `api/schemas/` (or equivalent API-schema module): at cutover, contains only `KarakeepBookmarkRef`; kept as a distinct narrow union so the OpenAPI request contract advertises exactly the kinds `IngressPolicy.accepted_submission_kinds` accepts.
  The internal `SourceRef` union (all six variants) remains unchanged — used for persistence, denormalization, manifests, and worker dispatch.
- [ ] Update API `JobSubmission` schema: remove `karakeep_id`, add required `source_ref: IngressSourceRef` field (narrow union)
- [ ] Update API `JobResponse` schema: add `source_ref: SourceRef` field (wide union — response reflects whatever is stored, even kinds that are not currently publicly submittable); retain `karakeep_id` as `str | None` (populated when `source_ref.kind == "karakeep_bookmark"`, null otherwise); keep existing `url: AnyUrl | None` and `title: str | None` field names unchanged
- [ ] Remove `karakeep_id` query parameter from `GET /v1/jobs`
- [ ] Add API kind gating via `SubmissionCapabilities` from `build_api_runtime`: return HTTP 422 when `source_ref.kind` is not in `SubmissionCapabilities.accepted_submission_kinds`.
  At cutover, `IngressPolicy.accepted_submission_kinds = {"karakeep_bookmark"}`; the worker registry still has `arxiv`/`github_readme`/`url`/`inline_html` registered for chain-closure validation, but those kinds return 422 at ingress because they are not publicly submittable.
  The request schema (narrow `IngressSourceRef`) already rejects non-submittable kinds at the pydantic-validation layer; the `SubmissionCapabilities` gate is a defense-in-depth check and the authoritative source if the narrow union ever widens without updating the policy.
- [ ] Update API job creation at `jobs.py:159-181`: materialize Source identity (compute `source_ref_hash` from `source_ref.to_dedup_payload()`, create/reuse Source row via `INSERT ... ON CONFLICT (source_ref_hash) DO NOTHING` followed by `SELECT`, populate `karakeep_id` only for `KarakeepBookmarkRef`); persist `source_ref` on the job record
- [ ] Update `compute_idempotency_key` signature to take `source_ref_hash`, `converter_name`, and a converter-supplied config snapshot (no Docling-specific fields); wire API submission path (`jobs.py:181`) to pass the configured converter's name and snapshot
- [ ] Create versioned manifest reader classes `ManifestV1` and `ManifestV2` (both with `model_config = ConfigDict(extra="forbid")`); add a version-dispatching loader that selects the reader class from the serialized `version` string
- [ ] Create `ManifestConfigSnapshotV2` pydantic model (`extra="forbid"`) with `converter_name: str` plus an opaque nested dict for adapter-supplied output-affecting fields
- [ ] Update manifest writer to emit `version = "2.0"`: emit two required typed ref blocks — `submitted_ref` (caller-supplied ingress ref) and `terminal_ref` (ref that produced the converted bytes), both drawn from the same `SourceRef` discriminated union; for direct submissions the two blocks carry equal values; make `ManifestSource.url`, `normalized_url`, `title`, `source_type`, `fetched_at` `str | None`; add `config_snapshot.converter_name`
- [ ] Ensure v1.0 readers remain available for legacy manifests; only v2.0 is written post-cutover
- [ ] Tests: Source row created with `KarakeepBookmarkRef`, `karakeep_id` populated; non-KaraKeep ref (once accepted in PR 7) produces null `karakeep_id`
- [ ] Tests: Source dedup — two identical `source_ref` submissions share one Source row; cosmetic ref differences (default fields, ordering) do not create new rows
- [ ] Tests: concurrent Source dedup — two simultaneous submissions with the same `source_ref_hash` result in exactly one Source row (via `INSERT ... ON CONFLICT DO NOTHING` + `SELECT`); both jobs FK to its `aizk_uuid`; job-level dedup proceeds via `idempotency_key`
- [ ] Tests: API accepts `source_ref` with `kind: "karakeep_bookmark"`; returns 422 for `kind: "url"` (registered in worker's `FetcherRegistry` but not in `IngressPolicy.accepted_submission_kinds` at cutover — proves the worker-internal-dispatch-kind-rejected-at-ingress scenario from the delta spec); returns 422 for `kind: "singlefile"` (not registered at all — the narrow `IngressSourceRef` union rejects it at pydantic-validation time, before the policy check)
- [ ] Tests: narrow `IngressSourceRef` union at the pydantic layer — a request body with `source_ref.kind = "arxiv"` fails schema validation (the request schema lists only `KarakeepBookmarkRef` at cutover), producing a 422 with a pydantic discriminator error distinct from the policy-gate 422
- [ ] Tests: API response includes `source_ref` and nullable `karakeep_id`; `karakeep_id` populated for KaraKeep jobs, null for others
- [ ] Tests: idempotency key differs for different `converter_name` with same source, and differs for different `source_ref_hash` with same converter
- [ ] Tests: `DoclingConverter.config_snapshot()` contributes the same output-affecting field set as today's Docling-specific config hash (structural dict equivalence; hash equivalence with pre-refactor keys is NOT asserted — the formula intentionally breaks)
- [ ] Tests: manifest v2.0 writer emits `converter_name`; for a direct `KarakeepBookmarkRef` job that terminated at KaraKeep, `submitted_ref` and `terminal_ref` are both `kind == "karakeep_bookmark"` with equal `bookmark_id`
- [ ] Tests: manifest v2.0 writer emits `submitted_ref.kind == "karakeep_bookmark"` (preserving the original `bookmark_id`) and `terminal_ref.kind == "arxiv"` (with `arxiv_id` populated) for a KaraKeep-to-arxiv job
- [ ] Tests: manifest v2.0 writer emits `submitted_ref == terminal_ref` (both `kind == "url"`) for a direct `UrlRef` submission
- [ ] Tests: `ManifestV2.model_config.extra == "forbid"` and `ManifestConfigSnapshotV2.model_config.extra == "forbid"` — unknown fields at read time raise
- [ ] Tests: version-dispatching loader returns `ManifestV1` instance for v1.0 JSON and `ManifestV2` instance for v2.0 JSON
- [ ] Tests: UI renders job list using `karakeep_id` from `JobResponse` (preserves today's UI behavior)
- [ ] Generate `schemas/after/` OpenAPI snapshot; verify the API portion of the diff matches `schemas/expected.md`
- [ ] Tests: OpenAPI `components.schemas` exposes two distinct discriminated unions — the narrow `IngressSourceRef` (at cutover: only `KarakeepBookmarkRef`) referenced by `JobSubmission.source_ref`, and the wide `SourceRef` (all six variants: `KarakeepBookmarkRef`, `ArxivRef`, `GithubReadmeRef`, `UrlRef`, `SingleFileRef`, `InlineHtmlRef`) referenced by `JobResponse.source_ref`; both carry a proper `discriminator` mapping on `kind`
- [ ] Docs alignment: grep the repo for references to `karakeep_id` in request-shape or query-param documentation (API reference docs, `CLAUDE.md` API section, README usage examples, architecture diagrams describing job submission), and update them to reflect `source_ref` as the submit-side identifier; retain `karakeep_id` documentation only where it describes the response-side compatibility field.
  Commit the doc touch-ups in the same PR so the API change and its documentation land together

## Stage 7 — BREAKING (behavior): Worker cutover to new orchestrator

- [ ] Replace worker's conversion loop to use `Orchestrator` from wiring (`build_worker_runtime`)
- [ ] Worker reads `source_ref` from job record instead of bookmark metadata for fetch dispatch
- [ ] Inject `ResourceGuard` into supervision/loop layer.
  The orchestrator SHALL enter the guard's `with` block only when the dispatched converter has `requires_gpu == True`; a converter with `requires_gpu == False` SHALL spawn without acquiring the guard.
  The acquiring worker thread SHALL wrap the full subprocess lifecycle (spawn + supervise + reap) in the `with guard:` block and SHALL be the sole releaser.
  The supervision loop SHALL NOT call `guard.release()` directly; on subprocess crash it surfaces failure to the acquiring thread whose `with` block unwinds
- [ ] Remove orchestrator/worker Source-creation code (API now owns identity materialization)
- [ ] Worker enriches the existing Source row's mutable metadata only (`url`, `normalized_url`, `title`, `source_type`, `content_type`) from fetcher/resolver chain results; never writes `aizk_uuid`, `source_ref`, `source_ref_hash`, or `karakeep_id`.
  `source_type` is set via `SOURCE_TYPE_BY_KIND[terminal_ref.kind]` (not emitted per-fetcher)
- [ ] Worker `DeploymentCapabilities.registered_kinds` already covers every adapter wired by `register_ready_adapters` (PR 5); no widening is required in this PR for worker dispatch.
  Public ingress remains narrow — `IngressPolicy.accepted_submission_kinds = {"karakeep_bookmark"}` at cutover.
  Widening public ingress to additional kinds (e.g., accepting `UrlRef` or `ArxivRef` directly) is a future config-only change to `IngressPolicy` (and a coordinated widening of the narrow `IngressSourceRef` request union), NOT an adapter change and NOT in scope for this PR.
- [ ] Update startup validation to use adapter-declared probes via wiring (aggregate `DeploymentCapabilities.startup_probes`).
  Deferred: adapter-declared probes are stubbed in `DeploymentCapabilities` (empty list) and existing startup validation in `api/startup.py` remains as-is.
  Enabling adapter-declared probes is a non-behavioral cleanup that can land independently.
- [ ] Tests: end-to-end worker processes a `KarakeepBookmarkRef` job through full pipeline (fetch → convert → upload).
  Deferred: requires docling in the local test env; exercised by the existing `tests/conversion/integration/test_conversion_flow.py` in the full CI environment
- [ ] Tests: worker does not attempt to create/update Source identity columns; only mutable metadata is written
- [ ] Tests: enrichment is best-effort-logged — when the Source-row UPDATE raises, the failure is logged with `aizk_uuid` and column set, the job proceeds to conversion, and the resulting manifest carries the authoritative values
- [ ] Tests: `source_type` on the Source row equals `SOURCE_TYPE_BY_KIND[terminal_ref.kind]` for each terminal kind — arxiv terminal → `"arxiv"`, github_readme terminal → `"github"`, url/karakeep_bookmark/inline_html terminal → `"other"`
- [ ] Tests: GPU guard semantics — a second worker thread attempting to acquire while another holds the guard blocks until the first thread's `with` block exits
- [ ] Tests: for a fake converter with `requires_gpu == False`, the orchestrator spawns the subprocess without entering the GPU guard's `with` block, and a concurrent GPU-bound job on another thread is not blocked by it
- [ ] Tests: idempotency key used by the worker equals the key computed API-side (no recomputation)
- [ ] Tests: startup probes are executed only for adapters registered by `register_ready_adapters`; skeleton classes that are not wired contribute no probes.
  Deferred alongside adapter-declared probes
- [ ] Tests: non-KaraKeep job (once the kind is publicly submittable) produces a v2.0 manifest with null source fields where the fetcher did not enrich them.
  Deferred: non-KaraKeep kinds are not publicly submittable because `IngressPolicy.accepted_submission_kinds = {"karakeep_bookmark"}` at cutover and `IngressSourceRef` advertises only `KarakeepBookmarkRef`.
  A future config-only change to `IngressPolicy` (widening the policy and the narrow request union together) enables e.g. `UrlRef` direct submission; the end-to-end test lands alongside that widening.
- [ ] Docs alignment: grep the repo for references to the legacy worker conversion loop, bookmark-based fetch dispatch, and the module-level GPU semaphore in source comments, module docstrings, `CLAUDE.md`, the worker section of architecture docs, and any runbook / on-call documentation; update them to reflect the injected `Orchestrator` + `ResourceGuard` + `source_ref`-driven dispatch.
  Commit the doc touch-ups in the same PR so the worker cutover and its documentation land together

## Stage 8 — Legacy module deletion (non-breaking)

- [ ] Remove re-exports from old module paths added in PRs 3-4 (`converter.py` re-export, `fetcher.py` re-export, `bookmark_utils.py` re-export, `arxiv_utils.py` re-export, `github_utils.py` re-export)
- [ ] Delete now-empty legacy modules if fully superseded
- [ ] Remove the old `if`/`elif` source-type dispatch ladder from old orchestrator code (if any remains)
- [ ] Remove module-level GPU semaphore (replaced by injected `ResourceGuard`)
- [ ] Verify no internal imports reference old module paths
- [ ] Tests: full test suite passes with no import warnings or deprecation notices

## Stage 9 — BREAKING (config): Env-var namespace rename

- [ ] Create per-adapter nested pydantic config models: `DoclingConverterConfig` under `AIZK_CONVERTER__DOCLING__*`, `KarakeepFetcherConfig` under `AIZK_FETCHER__KARAKEEP__*`, etc.
- [ ] Remove flat `AIZK_DOCLING_*` / `DOCLING_*` env-var aliases from config — no compatibility shim
- [ ] Update `.env.example` with new nested namespace structure
- [ ] Update adapter constructors to accept their nested config models
- [ ] Tests: `AIZK_CONVERTER__DOCLING__OCR_ENABLED=true` → `ocr_enabled` is `True`
- [ ] Tests: old `AIZK_DOCLING_OCR_ENABLED=true` with no nested equivalent → field falls back to default
- [ ] Tests: full test suite passes with new env-var names
- [ ] Update deployment configuration documentation/scripts with new env-var names
- [ ] Docs alignment: grep the repo for references to `AIZK_DOCLING_` / `DOCLING_` env-var names in source comments, module docstrings, `.env.example`, `CLAUDE.md`, configuration / deployment docs, and any runbook; update every occurrence to the nested `AIZK_CONVERTER__DOCLING__` / `AIZK_FETCHER__<ADAPTER>__` form.
  Commit the doc touch-ups in the same PR so the env-var rename and its documentation land together
