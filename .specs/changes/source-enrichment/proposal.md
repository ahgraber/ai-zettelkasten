# Proposal: source-enrichment

## Intent

The conversion pipeline turns bookmarks and URLs into Markdown artifacts persisted with metadata that identifies, attributes, and makes them retrievable.
Each stage — submit, resolve, fetch, convert, enrich, output — sees source information (title, canonical URL, publication date, author, …) that downstream consumers cannot reconstruct after the fact.

The pipeline has no structured channel to carry that information between stages.
The fetcher protocol returns an untyped `dict[str, Any]` that no adapter populates; resolvers cannot return metadata at all; the converter never extracts document-level fields; the subprocess IPC bridge carries only file paths; and enrichment writes only `source_type` and `content_type`, leaving `url`, `normalized_url`, and `title` permanently NULL despite the spec requiring all five.
The visible symptom today is the UI title column showing UUIDs, compounded by an inverted precedence check; the underlying problem is that title is one of many fields with nowhere to flow.

This change introduces a typed `SourceMetadata` struct that flows resolver → fetcher → converter → IPC → enrichment → `Source`/`ConversionOutput`, and closes the seven concrete gaps blocking the title use case.
Title and URL are the first fields it carries; future fields (publication date, author, language) extend the same channel without re-litigating the protocol.

---

## Data Flow & Stage Mapping

The process boundary matters: **resolve, fetch, AND convert all run in the conversion subprocess** (`_do_convert` body in `orchestrator.py`).
The parent process only sees what crosses `metadata.json`.
Every observation made anywhere in the subprocess must be serialised through that bridge to be usable by enrichment, manifest generation, or the API.

```text
═══════════════════════════════════════════════════════════════════════════════
  PARENT PROCESS                                                          API
═══════════════════════════════════════════════════════════════════════════════
┌──────────────────────────────────────────────────────────────────────────────┐
│  STAGE 1 · SUBMIT  (api routes)                                              │
│                                                                              │
│  POST /v1/jobs  { source_ref: { kind, bookmark_id / url / arxiv_id / … } }  │
│  → ConversionJob row (status=PENDING)                                        │
│  → Source row (aizk_uuid assigned; url=NULL, title=NULL)                     │
│  → job.title = source.karakeep_id OR str(aizk_uuid)  (placeholder)          │
└──────────────────────────────────────────────────────────────────────────────┘
                                  │ worker picks up job, spawns subprocess
                                  ▼
═══════════════════════════════════════════════════════════════════════════════
  CONVERSION SUBPROCESS  (everything below until metadata.json is written)
═══════════════════════════════════════════════════════════════════════════════
┌──────────────────────────────────────────────────────────────────────────────┐
│  STAGE 2 · RESOLVE  (orchestrator.process_with_provenance, in subprocess)   │
│                                                                              │
│  RefResolver.resolve(SourceRef) → (SourceRef, SourceMetadata)  [PROPOSED]   │
│                                                                              │
│  KarakeepBookmarkResolver observes:                                          │
│    bookmark.source_url ────────────► SourceMetadata.source_url  (canonical) │
│    normalize_url(source_url)  ─────► SourceMetadata.normalized_url          │
│    bookmark.title         ─────────► SourceMetadata.resolver_title          │
│    bookmark.source_url    ─────────► SourceMetadata.document_base_url       │
│  Returns (UrlRef(url=asset_url) | …, SourceMetadata)                         │
│  ─ note: ref.url is the asset retrieval URL; canonical URL stays in meta    │
│                                                                              │
│  Non-resolver sources: orchestrator synthesises SourceMetadata() (all None) │
└──────────────────────────────────────────────────────────────────────────────┘
                                  │ (terminal_ref, source_meta)
                                  ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  STAGE 3 · FETCH  (in subprocess)                                            │
│                                                                              │
│  ContentFetcher.fetch(ref, source_meta) → ConversionInput  [PROPOSED]       │
│                                                                              │
│  Fetcher MAY enrich source_meta with newly observed fields                  │
│  (e.g. canonical URL when called direct, not via resolver).                  │
│  Fetcher SHALL NOT overwrite a non-None field with None (merge semantics).  │
│                                                                              │
│  ConversionInput.source_meta: SourceMetadata  [replaces metadata: dict]     │
└──────────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  STAGE 4 · CONVERT  (DoclingConverter, in subprocess)                       │
│                                                                              │
│  DoclingConverter.convert(input) → ConversionArtifacts                      │
│    docling source = input.source_meta.document_base_url (HTML link base)    │
│    document_title = first TitleItem text (raw, no policy)                   │
│                                                                              │
│  ConversionArtifacts.document_title: str | None  [new — raw, not selected] │
└──────────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  STAGE 5 · TITLE SELECTION  (in subprocess, after convert)                  │
│                                                                              │
│  Selection runs where both raw inputs are visible:                          │
│    source_title = document_title  if non-empty AND not UUID-shaped           │
│                                       AND not bare URL                       │
│                  else source_meta.resolver_title                             │
│                  else None                                                   │
│                                                                              │
│  Output: SubprocessMetadata containing the SELECTED source_title            │
└──────────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  STAGE 6 · IPC BRIDGE  (subprocess writes typed SubprocessMetadata)         │
│                                                                              │
│  metadata.json now serialised from a typed SubprocessMetadata model:        │
│    pipeline_name, terminal_ref, content_type,                                │
│    markdown_filename, figure_files, docling_version, config_snapshot,       │
│    source_meta: { source_url, normalized_url,                               │
│                   document_base_url, resolver_title },                       │
│    document_title,                                                           │
│    source_title  ◄── selected value, AUTHORITATIVE for manifest/output      │
└──────────────────────────────────────────────────────────────────────────────┘
                                  │ subprocess exits; parent reads
                                  ▼
═══════════════════════════════════════════════════════════════════════════════
  PARENT PROCESS resumes
═══════════════════════════════════════════════════════════════════════════════
┌──────────────────────────────────────────────────────────────────────────────┐
│  STAGE 7a · UPLOAD / OUTPUT  (uploader._prepare_upload — AUTHORITATIVE)     │
│                                                                              │
│  Manifest values come from SubprocessMetadata, NOT from Source row:         │
│    manifest.source.url             ← subprocess_meta.source_meta.source_url │
│    manifest.source.normalized_url  ← subprocess_meta.source_meta.normalized_url │
│    manifest.source.title           ← subprocess_meta.source_title  (nullable)│
│  ConversionOutput.title (NOT NULL) ← subprocess_meta.source_title or       │
│                                       job.title  (placeholder fallback)     │
│                                                                              │
│  This satisfies the spec: "manifest's authoritative values remain correct   │
│  even if Source enrichment fails."                                           │
└──────────────────────────────────────────────────────────────────────────────┘
                                  │ parallel best-effort write
                                  ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  STAGE 7b · ENRICH  (_enrich_source_metadata — BEST-EFFORT CACHE)           │
│                                                                              │
│  Source.source_type        ◄── kind mapping              (unchanged)        │
│  Source.content_type       ◄── subprocess_meta.content_type (unchanged)     │
│  Source.url                ◄── subprocess_meta.source_meta.source_url       │
│  Source.normalized_url     ◄── subprocess_meta.source_meta.normalized_url   │
│  Source.title              ◄── subprocess_meta.source_title                 │
│                                                                              │
│  Failure is logged but does not affect manifest, output, or job status.     │
└──────────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  STAGE 8 · DISPLAY  (api routes + ui template)                              │
│                                                                              │
│  /v1/jobs/{id}: source.title or job.title       ✓ already correct          │
│  /ui/jobs:      source.title or job.title       (fix needed in ui.py)      │
│                 search SHALL cover Source.title (fix needed in ui.py)      │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## Scope

### In

- Introduce `SourceMetadata` — a `@dataclass(frozen=True)` carrying `source_url`,
  `normalized_url`, `document_base_url`, and `resolver_title`, with explicit `merge()`
  semantics (later stages enrich; non-`None` fields are never silently overwritten)
- Replace `ConversionInput.metadata: dict[str, Any]` with `source_meta: SourceMetadata`
- Change `RefResolver.resolve()` return type to `tuple[SourceRef, SourceMetadata]`
- Change `ContentFetcher.fetch()` signature to `fetch(ref, source_meta) -> ConversionInput`
- `KarakeepBookmarkResolver` populates `SourceMetadata` from the bookmark JSON:
  `source_url` from `bookmark.source_url` (the original page, **not** the asset URL the
  ref carries), `normalized_url` via `normalize_url()`, `document_base_url` from the same
  source URL (so docling resolves HTML links against the page that originally hosted
  them, not against the Karakeep asset path), `resolver_title` from `bookmark.title`
- All four content fetchers may enrich `source_meta` with newly-observed fields but
  SHALL NOT overwrite non-`None` values
- Add `document_title: str | None` to `ConversionArtifacts` (raw extraction; no
  selection policy in the converter)
- Title selection (`source_title`) lives in the subprocess after conversion, where both
  `document_title` and `source_meta.resolver_title` are visible: use `document_title`
  when non-empty, not UUID-shaped, and not a bare URL; otherwise `resolver_title`;
  otherwise `None`
- Introduce a typed `SubprocessMetadata` model (replaces the loose `metadata.json` dict)
  carrying terminal_ref, content_type, pipeline_name, artifact filenames, the final
  `SourceMetadata`, raw `document_title`, and the selected `source_title`
- `_prepare_upload` reads authoritative values from `SubprocessMetadata` (not from the Source row) when building the manifest and `ConversionOutput`.
  Manifest source-block field names (`url`, `normalized_url`, `title`) are unchanged; the `title` field may be `null`.
  `ConversionOutput.title` (NOT NULL column) is set to `subprocess_meta.source_title or job.title`, falling back to the submit-time placeholder so the non-null contract is preserved without a schema migration.
- `_enrich_source_metadata` reads the same `SubprocessMetadata` and writes the Source
  row as a separate best-effort cache write (existing failure semantics preserved)
- `Source.normalized_url` populated via `normalize_url()`; left `NULL` and logged when
  no usable `source_url` is observed
- Fix `/ui/jobs` route: precedence `source.title or job.title`; search clauses cover
  `Source.title` in addition to `ConversionJob.title`
- All six adapters explicitly inherit from their protocol class (`ContentFetcher`,
  `RefResolver`, `Converter`)

### Out

- New SourceRef kinds or new fetcher adapters
- Changing the `normalize_url()` algorithm itself (the existing `url-utils` contract is
  used as-is)
- Auth, rate-limiting, or any other API surface changes
- Changing how `job.title` is set at submission time (it remains a placeholder)
- Changing the `JobResponse` API contract (already correct; `jobs.py` route already
  uses the right precedence)

---

## Approach

**`SourceMetadata` dataclass with merge semantics.**
`@dataclass(frozen=True)` in `aizk.conversion.core.types` carrying: `source_url`, `normalized_url`, `document_base_url`, `resolver_title` — all `str | None = None`.
Provides `merge(other: SourceMetadata) -> SourceMetadata` which returns a new instance where each field is `self.<field> if self.<field> is not None else other.<field>` — i.e. earlier non-`None` observations win, later stages may fill in missing fields but never overwrite.
`ConversionInput.source_meta: SourceMetadata` replaces `metadata: dict[str, Any]`.
`ConversionArtifacts` gains `document_title: str | None = None` — raw, no policy.

**Resolver returns `(SourceRef, SourceMetadata)`.**
`KarakeepBookmarkResolver` is the only resolver today; it builds a populated `SourceMetadata` from the bookmark JSON (canonical URL, normalized URL, document-base URL, resolver title) and returns it alongside the resolved ref.
The asset URL stays in `ref.url` for the fetcher to retrieve bytes from; the canonical URL stays in `source_meta.source_url` for display, manifest, and link resolution.
Non-resolver entry paths produce `SourceMetadata()` (all `None`) at the orchestrator.

**Fetcher signature change.**
`ContentFetcher.fetch(ref, source_meta) -> ConversionInput`.
Fetchers may enrich `source_meta` with fields they observe (e.g. a direct `UrlRef` submission has no resolver, so `UrlFetcher` is responsible for setting `source_url` from `ref.url` and `normalized_url` from `normalize_url(ref.url)`).
Enrichment uses `source_meta.merge()` to preserve earlier observations.

**Converter emits raw observations only.**
`DoclingConverter` extracts the first `TitleItem` text as `document_title` (no policy).
Docling's `source=` parameter is set from `source_meta.document_base_url` — the page where relative links are anchored — falling back to `source_meta.source_url` if base URL is unset.

**Title selection in subprocess, after conversion.**
A small selection function reads `document_title` and `source_meta.resolver_title` and chooses `source_title` per the rule above.
This runs in the subprocess so the parent sees only the chosen value via `SubprocessMetadata.source_title`.

**Typed `SubprocessMetadata` IPC schema.**
A Pydantic (or dataclass) model defining the wire format of `metadata.json`.
Subprocess serialises an instance; parent deserialises into the same type.
Replaces the current loose `dict[str, Any]` reads in `_prepare_upload` and `_enrich_source_metadata`.

**Manifest reads from subprocess metadata, not Source row.**
`_prepare_upload` builds the manifest and `ConversionOutput.title` from `SubprocessMetadata` fields.
The Source row update becomes a separate best-effort write that reads the same `SubprocessMetadata`.
This realigns the implementation with the existing worker spec: "the manifest's authoritative values remain correct" even if the Source enrichment write fails.

**`normalize_url()` used properly.**
Source rows: `normalized_url = normalize_url(source_url)` when `source_url` is a valid URL; otherwise `NULL` and a debug log line.
Manifest reads `normalized_url` from `SubprocessMetadata.source_meta.normalized_url`.

**UI fix scoped to `ui.py` only.**
`/ui/jobs` route: flip precedence to `source.title or job.title`; extend the search clause in `_apply_filters` to include `Source.title`.
The `JobResponse` API contract and the `jobs.py` route are already correct — no changes.

**Explicit protocol inheritance.**
All six adapters (`KarakeepBookmarkResolver`, `ArxivFetcher`, `UrlFetcher`, `GithubReadmeFetcher`, `InlineContentFetcher`, `DoclingConverter`) declare their protocol as a base class.
No runtime behaviour change.
