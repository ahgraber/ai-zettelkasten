# Proposal: pluggable-fetch-convert

## Intent

The conversion pipeline currently hard-codes both ingestion source routing and Docling-based conversion.
Source type is detected inside the orchestrator via an `if`/`elif` ladder on KaraKeep bookmark metadata, and conversion is a direct import of Docling-specific functions with a flat, Docling-shaped configuration namespace.
This coupling makes it invasive to trial an alternative converter (e.g., Marker for PDF, a future image or office-doc converter), to accept content from a source other than a KaraKeep bookmark (e.g., webhook, SingleFile archive, direct URL), or to reason about conversion quality across alternatives.
This change establishes clean Ports & Adapters boundaries between the orchestrator core and the fetching/converting implementations, so future work — including quality A/B of alternative converters and new ingestion sources — is mechanical rather than invasive.

## Scope

**In scope:**

- Generalize `Bookmark` to `Source` as the canonical durable identity for anything the system can convert.
  Make `karakeep_id` nullable; add `source_ref` and `source_ref_hash` columns; backfill existing rows.
  Jobs continue to FK via `aizk_uuid`.
- Define `ContentFetcher`, `RefResolver`, and `Converter` protocols in a new core module.
- Define a `SourceRef` pydantic discriminated union with variants for the sources the system handles today (`karakeep_bookmark`, `arxiv`, `github_readme`, `url`, `singlefile`, `inline_html`) and a `ContentType` enum including today's formats and stubs for future ones (`pdf`, `html`, `image`, `docx`, `pptx`, `xlsx`, `csv`).
- Introduce `FetcherRegistry` and `ConverterRegistry` and a dependency-injected `Orchestrator` whose resolvers are backed by those registries.
- Split today's `converter.py` into a `DoclingConverter` adapter implementing the `Converter` protocol.
- Split today's `fetcher.py`, `bookmark_utils.py`, `arxiv_utils.py`, and `github_utils.py` into per-source adapter modules implementing `ContentFetcher` or `RefResolver`.
- Add role-specific composition builders (`build_worker_runtime`, `build_api_runtime`) that register adapters and wire the orchestrator.
- Preserve the parent-process GPU admission gate; refactor from a module-level semaphore to an injected `ResourceGuard` that remains above the subprocess boundary.
- Split the flat `ConversionConfig` into per-adapter nested pydantic sub-models (`cfg.converter.docling`, `cfg.fetcher.karakeep`, etc.) and migrate env-var names to nested form.
- Generalize the idempotency key and manifest `config_snapshot` to be converter-scoped rather than Docling-specific.
- Gate publicly submittable `source_ref.kind` values at the API layer via a `SubmissionCapabilities` descriptor sourced from an `IngressPolicy` config value — not raw registry membership and not an adapter class attribute — so public ingress is a deployment concern independent of worker-dispatch capability.
- Bump manifest to v2.0 with typed `submitted_ref` and `terminal_ref` blocks (both required) and nullable source-metadata fields, so non-KaraKeep jobs can serialize; v1.0 manifests remain readable.
- Preserve `JobResponse.karakeep_id` as a nullable convenience field (populated for KaraKeep jobs, null otherwise) so existing UI/consumers continue to work without a parallel UI migration.
- Preserve current S3 layout, retry semantics, and error codes.
  Manifest format bumps to v2.0 (readers accept both); output bytes and paths unchanged.

**Out of scope:**

- Implementing any alternative converter (Marker, MinerU, etc.).
- Implementing image / DOCX / PPTX / XLSX / CSV converter or fetcher adapters — enum variants are added but no adapters are registered for them.
- Implementing the webhook ingress path — `SourceRef` variants enable it but the HTTP surface is not changed.
- Experimentation infrastructure: canary routing, shadow mode, per-job converter selection at submit time, quality comparison metrics.
- Runtime converter selection beyond a single config/env-chosen implementation per deployment.
- Changes to subprocess supervision, signal handling, drain semantics, or the queue backpressure policy.
- Changes to the Conversion UI, MLflow tracing, URL utilities, or any unaffected capability spec.
  UI continues to read `karakeep_id` from `JobResponse` (retained as nullable); a UI migration to `source_ref` is deferred to a later change.

## Approach

- **Source entity**: the existing `bookmarks` table is generalized to `sources`.
  `karakeep_id` becomes nullable; `source_ref` (JSON) and `source_ref_hash` (unique, indexed) are added.
  Dedup is on `source_ref_hash` — two submissions with structurally identical `source_ref` share one Source row.
  `normalized_url` remains as derived metadata for human discovery and search, not dedup.
  `source_type` is retained as derived metadata (resolved semantic origin, e.g., "arxiv") distinct from `source_ref.kind` (ingress shape, e.g., "karakeep_bookmark").
  Jobs continue to FK via `aizk_uuid`; no new FK column is needed.
- **Single-owner Source identity**: the API materializes Source rows at submit time (validate `source_ref`, canonicalize, compute `source_ref_hash`, create/reuse row, persist job).
  The worker only enriches the existing Source row's mutable metadata (`url`, `normalized_url`, `title`, `source_type`, `content_type`) from the fetch chain.
  Source identity (`aizk_uuid`, `source_ref`, `source_ref_hash`, `karakeep_id`) is immutable after submit.
  This matches today's behavior — `jobs.py:159` already creates Bookmark rows API-side.
- **Canonical dedup payload**: `source_ref_hash` is computed from each `SourceRef` variant's `to_dedup_payload()` method — a normalized dict of only the fields that define semantic identity — rather than `model_dump_json()` (which would churn on field ordering, defaults, or cosmetic ref changes).
  Each variant owns its own dedup contract (e.g., `ArxivRef` normalizes the arxiv id; `InlineHtmlRef` hashes the body).
- **Ports & Adapters shape**: protocols and data types live in `aizk.conversion.core`; implementations live in `aizk.conversion.adapters/{converters,fetchers}`; role-specific builders at `aizk.conversion.wiring` are the only modules that import both layers.
  Core has no knowledge of Docling, KaraKeep, arxiv, etc.
- **Dependency injection**: the `Orchestrator` receives `FetcherResolver` and `ConverterResolver` callables via constructor injection.
  The registries satisfy those callable types.
  Tests pass fakes; production wires the registries.
- **Fetcher layer**: split into two role-distinct protocols: `ContentFetcher.fetch(ref) -> ConversionInput` (terminal — returns bytes) and `RefResolver.resolve(ref) -> SourceRef` (intermediate — refines a ref).
  The orchestrator recurses on resolved refs with a depth cap (default 2) to bound delegation — today's longest real chain is 1 hop, and cap 2 permits one unexpected extra level before failing loudly.
- **Converter layer**: capability-based registration.
  Each adapter declares `supported_formats: frozenset[ContentType]` and the registry is indexed by `(content_type, impl_name)`.
  One protocol rather than per-format protocols, because the expected format count (>=7) would make parallel per-format registries bookkeeping.
- **SourceRef storage**: `source_ref` is persisted on the Source row (canonical fetch instruction) and denormalized on the job record for the fetch chain.
  Typed on read via the discriminated union.
- **Inline-text bookmarks**: resolve to an `InlineHtmlRef` variant with the content embedded (size-capped; text bookmarks are typically \<50KB).
  Documented exception to the "refs are pointers" principle.
  `KarakeepBookmarkResolver` remains purely a resolver.
- **GPU concurrency**: the parent-process admission gate is preserved.
  The current `threading.Semaphore` acquired before subprocess spawn is the correct mechanism for cross-job GPU limiting.
  The refactor wraps it as a `ResourceGuard` protocol injected into the parent-side orchestration, not into converter adapters (which run in forked children).
  Adapter-level guards are reserved for optional intra-process subphases only.
- **Configuration**: per-adapter nested pydantic models replace the flat `docling_*` namespace.
  Env-var names change from `AIZK_DOCLING_*` to `AIZK_CONVERTER__DOCLING__*` in a hard break (sole user; no compatibility shim needed).
- **Idempotency key (API-side)**: the key hash incorporates the converter name plus the adapter's own output-affecting config snapshot, not a Docling-specific field list.
  `compute_idempotency_key` is called at submit time (`jobs.py:181`), so the formula change lands in PR 6b alongside the API schema change.
  Post-refactor keys do not match pre-refactor keys for the same content (two inputs change: `source_ref_hash` replaces the old `aizk_uuid`-based key component, and `converter_name` is newly incorporated).
  To keep replay-idempotency intact across the cutover, the PR 6a migration recomputes historical `idempotency_key` values in place using the new formula (see design.md).
- **Manifest evolution**: manifest `version` bumps from `"1.0"` to `"2.0"`.
  `karakeep_id` moves out of the top-level `ManifestSource` into the typed `submitted_ref` and/or `terminal_ref` blocks keyed on `source_ref.kind` (variants for `karakeep_bookmark`, `url`, `arxiv`, `github_readme`, `inline_html`).
  Both blocks are always present in v2.0 manifests; for direct submissions they carry equal values.
  `ManifestSource.url`, `normalized_url`, `title`, `source_type`, and `fetched_at` become `str | None` so non-KaraKeep jobs can serialize.
  `config_snapshot` gains `converter_name: str`.
  Readers accept both v1.0 and v2.0; v1.0 is read-only (no new v1.0 manifests written after PR 6).
- **API kind gating via a submission capability descriptor**: three concepts are kept distinct — worker dispatch (full `FetcherRegistry.registered_kinds()`), publicly submittable ingress (`IngressPolicy.accepted_submission_kinds`, a deployment-policy subset), and not-yet-wired future kinds (skeleton classes that are not registered at all).
  Wiring produces a `DeploymentCapabilities` descriptor for the worker and a separate `SubmissionCapabilities` descriptor for the API; the API validates `source_ref.kind` against `SubmissionCapabilities.accepted_submission_kinds`, not against registry membership and not against any adapter class attribute.
  Public-ingress policy lives in wiring config (an `IngressPolicy` value), not on adapter classes.
  Wiring enforces `accepted_submission_kinds ⊆ registered_kinds` at startup.
- **Role-specific wiring**: separate builders (`build_worker_runtime`, `build_api_runtime`, `build_test_runtime`) make startup probes, accepted source kinds, and registered adapters explicit per process role.
  One `wiring` package, multiple entry points.
- **Migration sequencing**: nine atomic PRs in dependency order.
  Structural and behavioral changes never mix in one PR.
  PRs marked **BREAKING** require coordinated updates to callers or deployment config.
  1. Core protocols, types, registries, `SourceRef` union — additive, non-breaking.
  2. `Orchestrator` class with fakes-based tests — additive, non-breaking.
  3. Docling adapter extraction — move + re-export, non-breaking.
  4. Fetcher adapter extraction — move + re-export, non-breaking.
  5. Wiring package with role-specific builders — additive, non-breaking. 6a.
     **BREAKING (schema)**: `bookmarks` → `sources` table generalization + Alembic migration.
     Make `karakeep_id` nullable; add `source_ref` (JSON) and `source_ref_hash` (unique-indexed) columns.
     Migration renames the table, backfills existing rows with `KarakeepBookmarkRef`, and recomputes historical `idempotency_key` values in place under the new formula.
     Downgrade is conditional — it aborts if any non-KaraKeep row exists, because the pre-migration schema cannot represent it (captured as a delta in `specs/schema-migrations/spec.md`). 6b.
     **BREAKING (API + manifest)**: JobSubmission/JobResponse cutover + manifest v2.0.
     `JobSubmission.source_ref` is a narrow `IngressSourceRef` discriminated union containing only the variants in `IngressPolicy.accepted_submission_kinds` (at cutover: only `KarakeepBookmarkRef`).
     `JobResponse.source_ref` carries the wide `SourceRef` union.
     `JobResponse.karakeep_id` retained as nullable compat field (populated for KaraKeep jobs, null otherwise) so the UI continues to render.
     API materializes Source rows and computes `source_ref_hash` via each variant's `to_dedup_payload()`.
     Idempotency key formula changes at this step (API-side, `jobs.py:181`) to include `converter_name` and the converter's config snapshot.
     Manifest writer bumps to v2.0 (nullable source fields, typed `submitted_ref` and `terminal_ref` blocks).
     `IngressPolicy.accepted_submission_kinds = {"karakeep_bookmark"}` at cutover; the worker registry still includes `arxiv`, `github_readme`, `url`, `inline_html` so resolver-chain closure validates.
  6. **BREAKING (behavior)**: worker cutover to new orchestrator.
     Reads `source_ref` from the job record instead of bookmark metadata; enriches mutable Source metadata from the fetch chain (does not create Source rows).
     Consumes the API-computed idempotency key unchanged.
     Widening public ingress later (e.g., accepting `UrlRef` directly) is a future config change to `IngressPolicy`, not a change in this rollout.
  7. Legacy module deletion — non-breaking (re-exports removed; all internal callers updated in PRs 6-7).
  8. **BREAKING (config)**: env-var namespace rename.
     `AIZK_DOCLING_*` replaced by `AIZK_CONVERTER__DOCLING__*`.
     Hard break; deployment env must be updated simultaneously.

## Schema Impact

- **`bookmarks` table renamed to `sources`**: `karakeep_id` becomes nullable; `source_ref` (JSON) and `source_ref_hash` (unique index) added.
  `source_type` retained as derived metadata.
  Existing rows backfilled with `KarakeepBookmarkRef`.
- **`POST /v1/jobs` request (`JobSubmission`)**: `karakeep_id` field removed; replaced by a required `source_ref` field typed as a narrow `IngressSourceRef` discriminated union containing only the variants in `IngressPolicy.accepted_submission_kinds`.
  At cutover, `IngressSourceRef` accepts only `KarakeepBookmarkRef`; widening is a future `IngressPolicy` config change.
  Accepted kinds gated by the `SubmissionCapabilities` descriptor (not registry membership, not adapter class attributes).
- **`JobResponse`**: `source_ref` added as the canonical source identifier, typed as the wide `SourceRef` discriminated union (all six variants) — the response surface reflects whatever the system actually stores, even for kinds that are not currently publicly submittable.
  `karakeep_id` retained as a nullable compat field — populated when `source_ref.kind == "karakeep_bookmark"`, null otherwise.
  Existing field `url: AnyUrl | None` unchanged (keep name — not renamed to `bookmark_url`).
  `title` unchanged; null for sources that have not yet been enriched or that lack a title.
- **Manifest format bump to v2.0**: `ManifestSource.url`, `normalized_url`, `title`, `source_type`, `fetched_at` become nullable.
  New typed `submitted_ref` and `terminal_ref` blocks (both required) keyed on `source_ref.kind` carry source-specific identifiers (e.g., `bookmark_id` for KaraKeep, `arxiv_id` for arxiv).
  For direct submissions (no resolver hop), `submitted_ref == terminal_ref`.
  `config_snapshot` adds `converter_name`.
  Readers accept both v1.0 and v2.0.
- **New schema components**: `SourceRef` discriminated union (oneOf on `kind`), plus individual ref schemas.
- **Unchanged**: all health endpoints, output endpoints, bulk actions, error shapes, S3 layout.
- **Removed query param**: `karakeep_id` filter removed from `GET /v1/jobs`. `aizk_uuid` filter remains.

See `schemas/expected.md` for full details.

## Open Questions

- **`source_ref` persistence form**: resolved — typed on read via the discriminated union (see design.md).
- **Env-var rename**: resolved — hard break (sole user; no compatibility shim).
- **Pre-ingress normalization**: in this change, KaraKeep ingress stores a `KarakeepBookmarkRef` and the resolver refines it at fetch time.
  A future optimization is to classify upstream and store the narrowed ref (`ArxivRef`, `UrlRef`, etc.) directly.
  Deferred to a later change.
