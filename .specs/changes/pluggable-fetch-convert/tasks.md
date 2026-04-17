# Tasks: pluggable-fetch-convert

## PR 1 — Core protocols, types, registries, SourceRef union (additive, non-breaking)

- [x] Create `aizk/conversion/core/__init__.py` package
- [x] Create `aizk/conversion/core/types.py`: `ContentType` enum (`pdf`, `html`, `image`, `docx`, `pptx`, `xlsx`, `csv`), `ConversionInput` (bytes + content_type + metadata), `ConversionArtifacts` (markdown + figures + metadata)
- [x] Create `aizk/conversion/core/source_ref.py`: `SourceRef` pydantic discriminated union with variants `KarakeepBookmarkRef`, `ArxivRef`, `GithubReadmeRef`, `UrlRef`, `SingleFileRef`, `InlineHtmlRef`; 64KB size cap on `InlineHtmlRef`
- [x] Implement `to_dedup_payload() -> dict` on each `SourceRef` variant — returns a canonical, normalized dict of identity-defining fields only (e.g., `KarakeepBookmarkRef → {"kind": "karakeep_bookmark", "bookmark_id": ...}`, `ArxivRef → {"kind": "arxiv", "arxiv_id": <normalized>}`, `UrlRef → {"kind": "url", "url": <normalized>}`, `InlineHtmlRef → {"kind": "inline_html", "content_hash": sha256(body)}`)
- [x] Add `compute_source_ref_hash(ref)` helper: SHA-256 of `json.dumps(ref.to_dedup_payload(), sort_keys=True, separators=(",", ":"))`
- [x] Create `aizk/conversion/core/protocols.py`: `ContentFetcher` protocol (`fetch(ref) -> ConversionInput`), `RefResolver` protocol (`resolve(ref) -> SourceRef`, `resolves_to: ClassVar[frozenset[str]]` class-level attribute enumerating every kind the resolver may emit), `Converter` protocol (`supported_formats: frozenset[ContentType]`, `requires_gpu: bool` class-level attribute, `convert(input) -> ConversionArtifacts`, `config_snapshot() -> dict`), `ResourceGuard` protocol (context manager; acquiring thread is sole releaser)
- [x] Create `aizk/conversion/core/registry.py`: `FetcherRegistry` with distinct registration entry points `register_content_fetcher(kind, impl)` and `register_resolver(kind, impl)` — role is declared at registration, not inferred from protocol shape; kind uniqueness enforced across both roles; `registered_kinds() -> frozenset[str]` returns the union of registered kinds; raises `FetcherNotRegistered` on unknown kind; `ConverterRegistry` (maps `(content_type, impl_name)` -> converter, raises `NoConverterForFormat`)
- [x] Create `aizk/conversion/core/errors.py`: `FetcherNotRegistered`, `NoConverterForFormat`, `FetcherDepthExceeded`, `ChainNotTerminated` typed errors with retryability classification (`ChainNotTerminated` is a startup-time error, not retryable)
- [x] Tests: `SourceRef` JSON round-trip for each variant; unknown kind rejected on deserialization; `InlineHtmlRef` exceeding 64KB rejected
- [x] Tests: `to_dedup_payload` — equivalent refs with cosmetic differences produce identical hashes; identity-field changes produce different hashes; `InlineHtmlRef` hash is content-addressed
- [x] Tests: `FetcherRegistry` — `register_content_fetcher` and `register_resolver` register the correct role; duplicate kind across roles is rejected; unregistered kind raises `FetcherNotRegistered`; `registered_kinds()` returns the union of both roles
- [x] Tests: `Converter.requires_gpu` is a class-level boolean inspectable without instantiation
- [x] Tests: `RefResolver.resolves_to` is a class-level `frozenset[str]` inspectable without instantiation
- [x] Tests: `ConverterRegistry` — register multi-format converter, resolve by `(content_type, name)`, missing combo error
- [x] Tests: `ContentType` enum has all 7 members

## PR 2 — Orchestrator class with fakes-based tests (additive, non-breaking)

- [x] Create `aizk/conversion/core/orchestrator.py`: `Orchestrator.__init__(resolve_fetcher, resolve_converter)` with DI callables; `_fetch(ref, depth)` with recursive dispatch and depth cap (default 3); `process(ref, converter_name) -> ConversionArtifacts`
- [x] Tests: orchestrator with fake content fetcher — single-hop fetch returns `ConversionInput`
- [x] Tests: orchestrator with fake ref resolver + content fetcher — two-hop resolution succeeds
- [x] Tests: depth limit exceeded raises `FetcherDepthExceeded`
- [x] Tests: orchestrator has no transitive import of any adapter module (inspect import graph)
- [x] Tests: orchestrator constructed with injected fakes completes fetch-convert cycle with no dependency on real adapters

## PR 3 — Docling adapter extraction (move + re-export, non-breaking)

- [x] Create `aizk/conversion/adapters/__init__.py` and `aizk/conversion/adapters/converters/__init__.py`
- [x] Create `aizk/conversion/adapters/converters/docling.py`: extract `DoclingConverter` from existing `converter.py`; implement `Converter` protocol with `supported_formats = frozenset({ContentType.PDF, ContentType.HTML})` and `requires_gpu = True`; supply `config_snapshot()` returning same output-affecting fields as today
- [x] Add re-export from old `converter.py` module path to avoid breaking internal imports
- [x] Tests: `DoclingConverter.supported_formats` contains `PDF` and `HTML`; `DoclingConverter.requires_gpu == True`
- [x] Tests: `DoclingConverter.config_snapshot()` returns the same field set as today's Docling-specific config hash
- [x] Tests: existing converter tests continue to pass (import path compatibility)

## PR 4 — Fetcher adapter extraction (move + re-export, non-breaking)

- [x] Create `aizk/conversion/adapters/fetchers/__init__.py`
- [x] Create `aizk/conversion/adapters/fetchers/karakeep.py`: extract `KarakeepBookmarkResolver` (implements `RefResolver`); preserve exact 7-step resolution precedence from `orchestrator.py:194-224`; return `ArxivRef`, `GithubReadmeRef`, `UrlRef`, or `InlineHtmlRef` as appropriate; declare `resolves_to = frozenset({"arxiv", "github_readme", "url", "inline_html"})`
- [x] Create `aizk/conversion/adapters/fetchers/arxiv.py`: extract `ArxivFetcher` (implements `ContentFetcher`); preserve 3-step PDF source precedence (KaraKeep asset → `arxiv_pdf_url` → abstract page resolution)
- [x] Create `aizk/conversion/adapters/fetchers/github.py`: extract `GithubReadmeFetcher` (implements `ContentFetcher`)
- [x] Create `aizk/conversion/adapters/fetchers/url.py`: extract `UrlFetcher` (implements `ContentFetcher`)
- [x] Create `aizk/conversion/adapters/fetchers/singlefile.py`: `SingleFileFetcher` skeleton class (implements `ContentFetcher`, raises `NotImplementedError`).
  Do NOT register it in the shared registration helper — the class exists for future work but is not wired into the registry until implemented, so `"singlefile"` does not appear in `accepted_kinds`
- [x] Create `aizk/conversion/adapters/fetchers/inline.py`: `InlineContentFetcher` (implements `ContentFetcher`); returns embedded bytes from `InlineHtmlRef` as `ConversionInput`
- [x] Add re-exports from old module paths (`fetcher.py`, `bookmark_utils.py`, `arxiv_utils.py`, `github_utils.py`)
- [x] Tests: `KarakeepBookmarkResolver` resolution precedence — arxiv bookmark returns `ArxivRef`, github returns `GithubReadmeRef`, PDF asset returns `UrlRef`, HTML content returns `UrlRef`, text-only returns `InlineHtmlRef`, empty returns error
- [x] Tests: `ArxivFetcher` PDF source precedence — asset preferred, `arxiv_pdf_url` fallback, abstract page resolution
- [x] Tests: `InlineContentFetcher` returns embedded bytes as `ConversionInput` with `ContentType.HTML`
- [x] Tests: existing fetcher/utils tests continue to pass (import path compatibility)

## PR 5 — Wiring package with role-specific builders (additive, non-breaking)

- [ ] Create `aizk/conversion/wiring/__init__.py`
- [ ] Create `aizk/conversion/wiring/capabilities.py`: `DeploymentCapabilities` descriptor with `accepted_kinds: frozenset[str]` (sourced directly from `FetcherRegistry.registered_kinds()`), `content_types_for(kind) -> frozenset[ContentType]`, `converter_available(content_type) -> bool`, `startup_probes: list[Probe]`.
  No `is_ready(kind)` or `resolver_chain_terminates(kind)` concepts
- [ ] Create `aizk/conversion/wiring/registrations.py` (or similar): `register_ready_adapters(fetcher_registry, converter_registry, cfg)` — the single source of truth for which adapters are wired.
  Called by both worker and API wiring so their `accepted_kinds` cannot drift.
  Skeleton adapters (e.g., `SingleFileFetcher`) are NOT called from this helper.
  After all registrations complete, the helper SHALL invoke `validate_chain_closure(fetcher_registry, depth_cap)` before returning; the check walks each resolver's `resolves_to` edges, asserts every produced kind is registered, and asserts the declared DAG has no cycles and no declared path exceeds the depth cap.
  On violation, raise `ChainNotTerminated` naming the resolver and missing kind (or the cycle); process startup fails before requests are accepted
- [ ] Create `aizk/conversion/wiring/worker.py`: `build_worker_runtime(cfg)` — calls `register_ready_adapters`, creates GPU `ResourceGuard` (wrapping `threading.Semaphore`), wires and returns `Orchestrator` + guard + `DeploymentCapabilities`
- [ ] Create `aizk/conversion/wiring/api.py`: `build_api_runtime(cfg)` — calls `register_ready_adapters` against its own registry instance and returns `DeploymentCapabilities` for request validation
- [ ] Create `aizk/conversion/wiring/testing.py`: `build_test_runtime(cfg)` — fake resolvers, in-memory registries, test-configurable registrations
- [ ] Verify: wiring package is the only package that imports both `core` and `adapters`
- [ ] Tests: `build_worker_runtime` registers all expected fetcher kinds and converter formats
- [ ] Tests: `build_api_runtime` and `build_worker_runtime` produce `DeploymentCapabilities` with identical `accepted_kinds` (shared registration helper)
- [ ] Tests: `"singlefile"` is not in `accepted_kinds` because `register_ready_adapters` does not wire it (skeleton class is not invoked)
- [ ] Tests: `validate_chain_closure` passes for the default wiring (KaraKeep resolver plus content fetchers for all four produced kinds)
- [ ] Tests: `validate_chain_closure` raises `ChainNotTerminated` when a resolver declares a `resolves_to` kind that is not registered (fixture drops one downstream fetcher)
- [ ] Tests: `validate_chain_closure` raises `ChainNotTerminated` when two resolvers declare a cycle in their `resolves_to` sets
- [ ] Tests: `validate_chain_closure` raises `ChainNotTerminated` when the declared resolver DAG has a path longer than the depth cap
- [ ] Tests: import graph lint — no adapter module imports `core` and another adapter

## PR 6 — BREAKING (schema + identity + manifest): Bookmark → Source generalization + API cutover

- [ ] Rename `datamodel/bookmark.py` → `datamodel/source.py`; rename class `Bookmark` → `Source`
- [ ] Make `karakeep_id` nullable on `Source`
- [ ] Add `source_ref` (JSON column) and `source_ref_hash` (unique indexed text column) to `Source`
- [ ] Add `source_ref` (JSON column) to `ConversionJob`
- [ ] Update `ConversionJob` FK from `bookmarks.aizk_uuid` → `sources.aizk_uuid`
- [ ] Write Alembic migration: rename `bookmarks` table → `sources`, add columns, backfill existing rows with `KarakeepBookmarkRef` and computed `source_ref_hash` (via `to_dedup_payload`)
- [ ] Update API `JobSubmission` schema: remove `karakeep_id`, add required `source_ref` field
- [ ] Update API `JobResponse` schema: add `source_ref` field; retain `karakeep_id` as `str | None` (populated when `source_ref.kind == "karakeep_bookmark"`, null otherwise); keep existing `url: AnyUrl | None` and `title: str | None` field names unchanged
- [ ] Remove `karakeep_id` query parameter from `GET /v1/jobs`
- [ ] Add API kind gating via `DeploymentCapabilities` from `build_api_runtime`: return HTTP 422 for kinds not in `accepted_kinds`; in this PR the descriptor reports `accepted_kinds = {"karakeep_bookmark"}` (other adapters not yet ready)
- [ ] Update API job creation at `jobs.py:159-181`: materialize Source identity (compute `source_ref_hash` from `source_ref.to_dedup_payload()`, create/reuse Source row via `INSERT ... ON CONFLICT (source_ref_hash) DO NOTHING` followed by `SELECT`, populate `karakeep_id` only for `KarakeepBookmarkRef`); persist `source_ref` on the job record
- [ ] Update `compute_idempotency_key` signature to take `source_ref_hash`, `converter_name`, and a converter-supplied config snapshot (no Docling-specific fields); wire API submission path (`jobs.py:181`) to pass the configured converter's name and snapshot
- [ ] Create versioned manifest reader classes `ManifestV1` and `ManifestV2` (both with `model_config = ConfigDict(extra="forbid")`); add a version-dispatching loader that selects the reader class from the serialized `version` string
- [ ] Create `ManifestConfigSnapshotV2` pydantic model (`extra="forbid"`) with `converter_name: str` plus an opaque nested dict for adapter-supplied output-affecting fields
- [ ] Update manifest writer to emit `version = "2.0"`: emit a typed `provenance` block describing the **terminal fetch state** (variants for `karakeep_bookmark`, `url`, `arxiv`, `github_readme`, `inline_html`); emit an optional `ingress` block **only when** the submitter-supplied ref differs from the terminal ref (e.g., KaraKeep→arxiv preserves `bookmark_id` under `ingress`); make `ManifestSource.url`, `normalized_url`, `title`, `source_type`, `fetched_at` `str | None`; add `config_snapshot.converter_name`
- [ ] Ensure v1.0 readers remain available for legacy manifests; only v2.0 is written post-cutover
- [ ] Update all internal references from `Bookmark` → `Source` (imports, type hints, variable names)
- [ ] Tests: Source row created with `KarakeepBookmarkRef`, `karakeep_id` populated; non-KaraKeep ref (once accepted in PR 7) produces null `karakeep_id`
- [ ] Tests: Source dedup — two identical `source_ref` submissions share one Source row; cosmetic ref differences (default fields, ordering) do not create new rows
- [ ] Tests: concurrent Source dedup — two simultaneous submissions with the same `source_ref_hash` result in exactly one Source row (via `INSERT ... ON CONFLICT DO NOTHING` + `SELECT`); both jobs FK to its `aizk_uuid`; job-level dedup proceeds via `idempotency_key`
- [ ] Tests: API accepts `source_ref` with `kind: "karakeep_bookmark"`, returns 422 for `kind: "url"` and for `kind: "singlefile"` (neither registered by `register_ready_adapters` in this PR)
- [ ] Tests: API response includes `source_ref` and nullable `karakeep_id`; `karakeep_id` populated for KaraKeep jobs, null for others
- [ ] Tests: Source identity columns (`aizk_uuid`, `source_ref`, `source_ref_hash`, `karakeep_id`) are immutable after creation
- [ ] Tests: idempotency key differs for different `converter_name` with same source, and differs for different `source_ref_hash` with same converter
- [ ] Tests: `DoclingConverter.config_snapshot()` contributes the same output-affecting field set as today's Docling-specific config hash (structural dict equivalence; hash equivalence with pre-refactor keys is NOT asserted — the formula intentionally breaks)
- [ ] Tests: manifest v2.0 writer emits `converter_name`, and for a KaraKeep-terminal job emits `provenance.kind == "karakeep_bookmark"` with `bookmark_id` and omits `ingress`
- [ ] Tests: manifest v2.0 writer emits `provenance.kind == "arxiv"` and `ingress.kind == "karakeep_bookmark"` (preserving `bookmark_id`) for a KaraKeep-to-arxiv job
- [ ] Tests: `ManifestV2.model_config.extra == "forbid"` and `ManifestConfigSnapshotV2.model_config.extra == "forbid"` — unknown fields at read time raise
- [ ] Tests: version-dispatching loader returns `ManifestV1` instance for v1.0 JSON and `ManifestV2` instance for v2.0 JSON
- [ ] Tests: UI renders job list using `karakeep_id` from `JobResponse` (preserves today's UI behavior)
- [ ] Tests: migration backfill — existing bookmark rows have valid `source_ref` and `source_ref_hash`
- [ ] Generate `schemas/after/` OpenAPI snapshot; verify diff matches `schemas/expected.md`

## PR 7 — BREAKING (behavior): Worker cutover to new orchestrator

- [ ] Replace worker's conversion loop to use `Orchestrator` from wiring (`build_worker_runtime`)
- [ ] Worker reads `source_ref` from job record instead of bookmark metadata for fetch dispatch
- [ ] Inject `ResourceGuard` into supervision/loop layer.
  The orchestrator SHALL enter the guard's `with` block only when the dispatched converter has `requires_gpu == True`; a converter with `requires_gpu == False` SHALL spawn without acquiring the guard.
  The acquiring worker thread SHALL wrap the full subprocess lifecycle (spawn + supervise + reap) in the `with guard:` block and SHALL be the sole releaser.
  The supervision loop SHALL NOT call `guard.release()` directly; on subprocess crash it surfaces failure to the acquiring thread whose `with` block unwinds
- [ ] Remove orchestrator/worker Source-creation code (API now owns identity materialization)
- [ ] Worker enriches the existing Source row's mutable metadata only (`url`, `normalized_url`, `title`, `source_type`, `content_type`) from fetcher/resolver chain results; never writes `aizk_uuid`, `source_ref`, `source_ref_hash`, or `karakeep_id`
- [ ] Widen `DeploymentCapabilities.accepted_kinds` by adding additional adapters to the `register_ready_adapters` helper as they become ready (e.g., `arxiv`, `url`, `github_readme`); API surface grows as the helper registers more kinds
- [ ] Update startup validation to use adapter-declared probes via wiring (aggregate `DeploymentCapabilities.startup_probes`)
- [ ] Tests: end-to-end worker processes a `KarakeepBookmarkRef` job through full pipeline (fetch → convert → upload)
- [ ] Tests: worker does not attempt to create/update Source identity columns; only mutable metadata is written
- [ ] Tests: for a converter with `requires_gpu == True` (`DoclingConverter`), the GPU guard is acquired in the parent thread before subprocess spawn, held through subprocess reap, and released when the acquiring thread's `with` block exits — including the subprocess-crash path (no direct `guard.release()` from the supervision loop)
- [ ] Tests: a second worker thread attempting to acquire while another holds the guard blocks until the first thread's `with` block exits
- [ ] Tests: for a fake converter with `requires_gpu == False`, the orchestrator spawns the subprocess without entering the GPU guard's `with` block, and a concurrent GPU-bound job on another thread is not blocked by it
- [ ] Tests: idempotency key used by the worker equals the key computed API-side (no recomputation)
- [ ] Tests: startup probes are executed only for adapters registered by `register_ready_adapters`; skeleton classes that are not wired contribute no probes
- [ ] Tests: non-KaraKeep job (once the kind is ready) produces a v2.0 manifest with null source fields where the fetcher did not enrich them

## PR 8 — Legacy module deletion (non-breaking)

- [ ] Remove re-exports from old module paths added in PRs 3-4 (`converter.py` re-export, `fetcher.py` re-export, `bookmark_utils.py` re-export, `arxiv_utils.py` re-export, `github_utils.py` re-export)
- [ ] Delete now-empty legacy modules if fully superseded
- [ ] Remove the old `if`/`elif` source-type dispatch ladder from old orchestrator code (if any remains)
- [ ] Remove module-level GPU semaphore (replaced by injected `ResourceGuard`)
- [ ] Verify no internal imports reference old module paths
- [ ] Tests: full test suite passes with no import warnings or deprecation notices

## PR 9 — BREAKING (config): Env-var namespace rename

- [ ] Create per-adapter nested pydantic config models: `DoclingConverterConfig` under `AIZK_CONVERTER__DOCLING__*`, `KarakeepFetcherConfig` under `AIZK_FETCHER__KARAKEEP__*`, etc.
- [ ] Remove flat `AIZK_DOCLING_*` / `DOCLING_*` env-var aliases from config — no compatibility shim
- [ ] Update `.env.example` with new nested namespace structure
- [ ] Update adapter constructors to accept their nested config models
- [ ] Tests: `AIZK_CONVERTER__DOCLING__OCR_ENABLED=true` → `ocr_enabled` is `True`
- [ ] Tests: old `AIZK_DOCLING_OCR_ENABLED=true` with no nested equivalent → field falls back to default
- [ ] Tests: full test suite passes with new env-var names
- [ ] Update deployment configuration documentation/scripts with new env-var names
