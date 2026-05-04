# Design: source-enrichment

## Context

- The pluggable pipeline separates **resolvers** (refine a `SourceRef`) from **content fetchers** (return bytes).
  This split underwrites chain-closure validation, registry dispatch, and `source_ref_hash` deduplication.
  The metadata channel must accommodate it without collapsing the distinction.
- **Resolve, fetch, AND convert all execute inside the conversion subprocess** (`_do_convert` body in `orchestrator.py`).
  The parent process only sees what crosses the `metadata.json` IPC bridge.
  Anything observed in the subprocess that the parent needs — for the manifest, for `ConversionOutput`, for Source enrichment — must traverse this bridge.
- The conversion-worker spec already states that the manifest is authoritative and that Source enrichment is a best-effort cache.
  The current implementation contradicts this: `_prepare_upload` reads `source.url` and `source.title or job.title` directly from the Source row, which means a failed enrichment write produces a wrong manifest.
- `Source` rows are an advisory cache for UI/search under last-writer-wins.
  The cache must be writable independently of the manifest; the manifest must not depend on it.
- `SourceRef` variants are identity / fetch-instruction values used as registry keys and hash inputs.
  Their shape is part of the dedup contract.
- The `url-utils` capability owns `normalize_url()` with a defined contract for canonicalisation.
  Source-row `normalized_url` and manifest `normalized_url` should both come from this function, not from a passthrough.

## Decisions

### D1. Resolver returns `tuple[SourceRef, SourceMetadata]`; `SourceRef` stays pure

`SourceRef` variants remain identity / fetch-instruction structs (used by registry dispatch and `source_ref_hash`).
Source-descriptive fields (canonical URL, normalized URL, document base URL, resolver title) flow exclusively through the companion `SourceMetadata` value.

The Karakeep case makes the distinction concrete: the resolver returns `UrlRef(url=karakeep_asset_url)` because that is where the fetcher must retrieve bytes from.
The original page URL the user bookmarked stays in `SourceMetadata.source_url` — used for display, the manifest, and as docling's base URL for relative link resolution.
If `source_url` were stored on `SourceRef`, identity hashing would conflate "where bytes live" with "what page this represents," and resolver retries against a moved asset would change the dedup key.

**Alternatives considered:**

- **Optional descriptive fields on `SourceRef` subclasses** — rejected: pollutes identity, changes hash inputs, every adapter must defensively read them.
- **Fetcher re-fetches Karakeep bookmark** — rejected: extra API call, only works for Karakeep, no path for direct submissions.
- **Orchestrator-side context store** — rejected: hidden coupling, no type safety at the call boundary.

### D2. `SourceMetadata` carries four fields with `merge()` semantics

Fields: `source_url`, `normalized_url`, `document_base_url`, `resolver_title` — all `str | None = None`.

`merge(other) -> SourceMetadata` returns a new instance where each field is `self.<field> if self.<field> is not None else other.<field>`.
"Earlier non-`None` wins" is the correct semantic because the resolver sees the most authoritative source identity (the original Karakeep bookmark URL), and downstream stages should fill in fields the resolver could not observe — they should not override the resolver.
For non-resolver paths the orchestrator synthesises an empty `SourceMetadata()` so the fetcher's observations are the first non-`None` values.

`document_base_url` and `source_url` are kept as separate fields because they can diverge: a Karakeep-archived HTML page where the bookmark URL is the original page but the bytes were retrieved from `/api/v1/assets/{id}` should resolve relative `<img src="/foo.png">` against the original page, not against the asset path.
Default behaviour for fetchers without resolver context: set both to the same value.

`@dataclass(frozen=True)`, not Pydantic — no validation rules, no coercion, lighter import in the subprocess hot path, `dataclasses.asdict` serialises cleanly.

### D3. Title selection lives in the subprocess, not the converter or the parent

The converter emits `document_title` as a raw observation; it does not know about resolver titles or fallback policy.
The parent process only sees the chosen value (via `SubprocessMetadata.source_title`); it does not re-derive it.

Selection runs in the subprocess after conversion, where both `document_title` and `source_meta.resolver_title` are visible:

- Use `document_title` if non-empty, not UUID-shaped (`[0-9a-f]{8}-...-[0-9a-f]{12}` or hex-and-dash 32-36 chars), and not starting with `http://` or `https://`.
- Otherwise use `resolver_title` if non-empty.
- Otherwise `None`.

The rule is heuristic because Docling's `TitleItem` extraction sometimes captures a UUID from a URL fragment, an empty heading, or the URL itself.
The Karakeep `bookmark.title` is human-curated or AI-generated and is usually a better human-readable name than a low-confidence extraction.
When extraction does succeed, it's authoritative.

**Why not in the converter:** the converter spec says converters report observations, not policy.
A different converter (future Pandoc, future structured PDF) should not have to re-implement the same selection rule.

**Why not in the parent:** the parent reads `SubprocessMetadata` and writes the manifest in one shot.
Putting selection in the parent would mean shipping both `document_title` and `resolver_title` over IPC and re-implementing the rule there.
Doing selection in the subprocess makes `SubprocessMetadata.source_title` the single authoritative value across manifest, output, and Source-row enrichment.

### D4. Typed `SubprocessMetadata` model replaces the loose `metadata.json` dict

Today `_do_convert` writes a `dict[str, Any]` and `_prepare_upload` reads keys via `.get()`.
The schema is implicit and drifts silently.

Define a `SubprocessMetadata` Pydantic model (validation matters here — this is a trust boundary between processes):

- existing fields: `pipeline_name`, `terminal_ref`, `content_type`, `markdown_filename`, `figure_files`, `markdown_hash_xx64`, `docling_version`, `config_snapshot`, `fetched_at`
- new fields: `source_meta: SourceMetadata`, `document_title: str | None`, `source_title: str | None`

Both sides serialise/deserialise through the model with `extra="forbid"` so unknown fields fail loudly.
This is the appropriate place for Pydantic (validation across a trust boundary), in contrast to D2 where `SourceMetadata` is a plain dataclass (in-process value type).

### D5. Manifest reads from `SubprocessMetadata`, not the Source row

`_prepare_upload` is rewritten to construct the manifest from `SubprocessMetadata` directly:

```text
manifest.source.url             ← subprocess_meta.source_meta.source_url
manifest.source.normalized_url  ← subprocess_meta.source_meta.normalized_url
manifest.source.title           ← subprocess_meta.source_title         (may be null)
ConversionOutput.title          ← subprocess_meta.source_title or job.title
```

Manifest source-block field names (`url`, `normalized_url`, `title`) are unchanged from v2.0; only the value source changes.
`ConversionOutput.title` falls back to the parent `ConversionJob.title` placeholder because the column is currently `nullable=False` ([output.py:20](src/aizk/conversion/datamodel/output.py#L20)) — preserving the non-null contract avoids a schema/API migration that would be out of scope for this change.
A future change can make the column nullable if it's worth the migration.

The Source-row UPDATE moves to a separate function (or a separate `with Session()` block in `_prepare_upload`) that reads the same `SubprocessMetadata` and writes the row best-effort.
Failure of the UPDATE is logged but does not affect manifest, output, or job completion — exactly what the existing worker spec already requires but the implementation does not currently honour.

### D6. `normalize_url()` used at observation time, in the subprocess

The Karakeep resolver and any direct `UrlFetcher` call `normalize_url(source_url)` when `source_url` is a syntactically valid URL.
The result lives on `SourceMetadata.normalized_url` and flows through the same channel as `source_url`.

When `source_url` is `None` or the URL fails validation, `normalized_url` is `None` and a debug log line records the absence (for forensic visibility into `inline_html` vs URL-validation-failure cases).
The job does not fail.

The previous proposal's `normalized_url = url` passthrough is removed.
Doing real normalisation at observation time avoids two failure modes: (a) the manifest carrying a non-canonical URL different from `Source.normalized_url`, and (b) future dedup queries against `normalized_url` missing matches because half the rows hold canonical values and half hold passthroughs.

### D7. UI delta: search must cover `Source.title`; precedence flip in `ui.py`

Two related fixes in `api/routes/ui.py`:

- Row construction: `"title": source.title or job.title or ""` (was: `job.title or source.title or ""`).
- Search clause in `_apply_filters`: add `func.lower(Source.title).like(pattern)` to the `or_` group (currently only `ConversionJob.title` is searched).

The `JobResponse` API contract (`api/routes/jobs.py`) is already correct: it uses `source.title or job.title` and the `JobResponse` spec already says `title` is the enriched value.
No `conversion-api` delta is needed.

### D8. Explicit protocol inheritance for all six adapters

`KarakeepBookmarkResolver`, `ArxivFetcher`, `UrlFetcher`, `GithubReadmeFetcher`, `InlineContentFetcher`, `DoclingConverter` declare their protocol as a base class.
No runtime change — `@runtime_checkable` already drives dispatch — but it makes intent visible at the class definition, catches signature drift at type-check time, and surfaces protocol docstrings to editors.

Bundled here because all six adapters are touched in this PR for the protocol signature changes anyway; a separate cleanup pass is more churn than the change itself.

## Architecture

### Process boundary and metadata flow

```text
═══════════ PARENT ═══════════                    ═══════════ SUBPROCESS ═══════════

api routes ──► submit job
                                          spawn
worker dispatcher ────────────────────────────────► orchestrator.process_with_provenance
                                                         │
                                                         │ resolver.resolve(ref)
                                                         │   → (ref', meta_resolver)
                                                         │
                                                         │ for each fetcher hop:
                                                         │   meta = meta.merge(prior)
                                                         │   cinput = fetcher.fetch(ref, meta)
                                                         │
                                                         │ artifacts = converter.convert(cinput)
                                                         │   → document_title (raw)
                                                         │
                                                         │ source_title = select(
                                                         │     artifacts.document_title,
                                                         │     cinput.source_meta.resolver_title)
                                                         │
                                                         ▼
                                                  SubprocessMetadata.write(metadata.json)
                                                         │
       ◄─────────────────────────────────────────── (subprocess exits)
       │
       ▼
  meta = SubprocessMetadata.read(metadata.json)        ┐
       │                                                │
       ├──► uploader._prepare_upload(meta)              │  authoritative path
       │      manifest.{url, normalized_url, title}     │
       │      ConversionOutput.title                    │
       │      ← all read from `meta`                    │
       │                                                ┘
       │
       └──► _enrich_source_metadata(meta)               ┐  best-effort cache
              UPDATE Source SET                         │
                url=…, normalized_url=…, title=…       │  (failure logged, not raised)
              ← all read from same `meta`               ┘
```

### Type relationships

```text
   SourceRef (identity, hashable)              SourceMetadata (descriptive, frozen + merge)
   ┌──────────────────────────────┐            ┌──────────────────────────────┐
   │ KarakeepBookmarkRef          │            │ source_url        : str|None │
   │ UrlRef                       │            │ normalized_url    : str|None │
   │ ArxivRef                     │            │ document_base_url : str|None │
   │ GithubReadmeRef              │            │ resolver_title    : str|None │
   │ InlineHtmlRef                │            │  …(future fields)            │
   └──────────────────────────────┘            │ + merge(other)               │
            │                                  └──────────────────────────────┘
            │                                              │
            │       paired by orchestrator/fetcher         │
            └───────────────────┬──────────────────────────┘
                                ▼
                  ConversionInput { content, content_type, source_meta }
                                │
                                ▼ DoclingConverter.convert
                  ConversionArtifacts { markdown, figures, document_title }
                                │
                                ▼ subprocess title-selection
                  SubprocessMetadata {
                      pipeline_name, terminal_ref, content_type,
                      markdown_filename, figure_files, …,
                      source_meta,        ◄── final merged SourceMetadata
                      document_title,     ◄── raw from converter
                      source_title,       ◄── selected, authoritative
                  }
                                │
                                ▼ metadata.json
                          (process boundary)
                                │
                                ▼ parent reads
                  → manifest + ConversionOutput   (authoritative)
                  → Source row UPDATE             (best-effort cache)
```

### Resolver / fetcher dispatch (within subprocess)

```text
        orchestrator
             │
             │  source_meta = SourceMetadata()  # all None
             │  impl = registry.resolve(ref.kind)
             ▼
   ┌─────────────────────────┐
   │ isinstance(impl,        │
   │   RefResolver)?         │
   └────────┬────────────────┘
       yes  │              │  no
            ▼              ▼
   ref, meta_new =   cinput =
   impl.resolve(ref) impl.fetch(ref, source_meta)
            │              │
   source_meta =           ▼
     source_meta           cinput.source_meta is the
       .merge(meta_new)    final merged metadata
            │
            └──► loop until terminal (bounded by depth_cap)
```

## Risks

### R1. Title-selection rule is heuristic; can mis-classify unusual real titles

A real document titled `"https://example.com/why-urls-are-titles"` would be rejected by the bare-URL check.
A title that happens to be a 32-character hex-and-dash string (vanishingly rare in practice) would be rejected as UUID-shaped.

**Mitigation:** these are pathological cases.
Fallback to `resolver_title` preserves Karakeep titles in the common Docling-misfires case.
Fallback to `None` preserves existing behaviour where nothing better is known.
Tighten the heuristic later if real misses appear.

### R2. `SubprocessMetadata` becomes a load-bearing IPC contract

Once parent and subprocess agree on a typed schema, every change to it must be coordinated across both sides and cannot drift silently.
Mis-versioned deploys (parent on N+1, subprocess on N) would trip `extra="forbid"` validation and fail jobs.

**Mitigation:** parent and subprocess ship from the same package — there is no independent deploy.
A future cross-version deployment story would need explicit versioning of the IPC schema, but that's not in scope here.
The benefit (catching drift loudly instead of silently dropping fields) outweighs the constraint.

### R3. Manifest schema gains optional fields without a version bump

`source_url`, `normalized_url`, and `title` on the v2.0 manifest source block are populated more reliably (and from a different source) than before.
Existing v2.0 readers with `extra="forbid"` should not be affected because the field names are unchanged, but a reader that previously expected these to be `null` for non-Karakeep sources will now see real values.

**Mitigation:** in-repo only; no external manifest consumers.
If one appears later, bump to v2.1.

### R4. Karakeep `document_base_url` may not match where the page was archived

If a bookmark's `source_url` is `https://blog.example.com/post`, but the asset stored at Karakeep is a copy with rewritten asset paths, docling resolving relative URLs against `https://blog.example.com/post` may produce broken image links.

**Mitigation:** acceptable for now — broken external image links are no worse than the current behaviour where docling has no base URL at all.
A follow-up could detect this case and prefer the asset URL, but the contract here keeps the field set consistent with how a bookmark is conceptually identified (the original page).

### R5. Real `normalize_url()` may reject URLs the previous passthrough accepted

If `normalize_url()` raises on certain inputs (e.g. malformed URLs), the worker now logs and stores `NULL` instead of the original string.
Loses some forensic value (original URL is still in `Source.url` and `manifest.url`) but improves dedup correctness.

**Mitigation:** the worker spec change explicitly allows `normalized_url = NULL` with a log line; no failure mode introduced.
If the strict behaviour is too aggressive in practice, wrap the call to capture the exception and store `NULL` plus a typed log field.

## Verification Overrides

| Field              | Value                                                                                                                                                                                  |
| ------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Finding**        | `tests/test_pypi_security_audit.py::test_pip_audit_no_vulnerabilities` fails — `litellm 1.83.0` has known vulnerability `GHSA-xqmj-j6mv-4862`; fix is published in `litellm 1.83.7`    |
| **Stage**          | verify                                                                                                                                                                                 |
| **Reason**         | The fixed version (`litellm ≥ 1.83.7`) is not yet installable within the current `uv`-managed dependency solution space; no compatible version exists in the lockfile as of 2026-05-04 |
| **Constraints**    | Do NOT add a `pip audit` ignore entry; this must be remediated once a compatible fixed version becomes installable                                                                     |
| **Follow-up task** | Upgrade `litellm` to ≥ 1.83.7 and re-run `pip audit` once dependency constraints permit                                                                                                |
| **Approved by**    | User (explicit approval recorded in `tasks.md` final verification item)                                                                                                                |
| **Recorded**       | 2026-05-04                                                                                                                                                                             |
