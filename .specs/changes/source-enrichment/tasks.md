# Tasks: source-enrichment

## Core types

- [ ] Add `SourceMetadata` `@dataclass(frozen=True)` to `aizk.conversion.core.types` with fields `source_url`, `normalized_url`, `document_base_url`, `resolver_title` (all `str | None = None`)
- [ ] Implement `SourceMetadata.merge(other: SourceMetadata) -> SourceMetadata` with field-wise "earlier non-`None` wins" semantics
- [ ] Replace `ConversionInput.metadata: dict[str, Any]` with `source_meta: SourceMetadata = Field(default_factory=SourceMetadata)` (Pydantic `Field`, since `ConversionInput` is `BaseModel`)
- [ ] Add `document_title: str | None = None` to `ConversionArtifacts`
- [ ] Add `SubprocessMetadata` Pydantic model in `aizk.conversion.workers.types` (or a new `ipc.py`) with `extra="forbid"`; fields: existing `metadata.json` keys plus `source_meta`, `document_title`, `source_title`

## Protocols

- [ ] Update `ContentFetcher.fetch()` signature in `aizk.conversion.core.protocols` to `fetch(ref: SourceRef, source_meta: SourceMetadata) -> ConversionInput`
- [ ] Update `RefResolver.resolve()` return type in `aizk.conversion.core.protocols` to `tuple[SourceRef, SourceMetadata]`
- [ ] Make all six adapters declare their protocol as a base class: `ContentFetcher` for `ArxivFetcher`, `UrlFetcher`, `GithubReadmeFetcher`, `InlineContentFetcher`; `RefResolver` for `KarakeepBookmarkResolver`; `Converter` for `DoclingConverter`

## Resolver

- [ ] Update `KarakeepBookmarkResolver.resolve()` to compute `SourceMetadata` from the bookmark JSON: `source_url` and `document_base_url` from `bookmark.source_url`, `normalized_url = normalize_url(source_url)` when valid, `resolver_title = bookmark.title`
- [ ] Update `KarakeepBookmarkResolver.resolve()` return statement to `return (resolved_ref, source_metadata)` for all four branches (arxiv / github / pdf-asset / archive-asset / inline)

## Fetchers

- [ ] Update `UrlFetcher.fetch()` for new signature; on direct submission (no resolver hop) populate `source_meta` with `source_url=ref.url`, `document_base_url=ref.url`, `normalized_url=normalize_url(ref.url)` via `merge()`; on resolved invocation, preserve resolver-supplied fields
- [ ] Update `ArxivFetcher.fetch()` for new signature; populate `source_meta` with `source_url` (ArXiv abstract page URL), `document_base_url` (same), and `normalized_url=normalize_url(source_url)` via `merge()` when the resolver did not already set them
- [ ] Update `GithubReadmeFetcher.fetch()` for new signature; populate `source_meta` with `source_url` (constructed README URL), `document_base_url` (same), and `normalized_url=normalize_url(source_url)` via `merge()` when the resolver did not already set them
- [ ] Update `InlineContentFetcher.fetch()` for new signature; pass `source_meta` through unchanged (no URL to observe)

## Converter

- [ ] Update `DoclingConverter.convert()` to extract first `TitleItem` text from `conv_result.document.texts` and set `artifacts.document_title` (raw, no policy)
- [ ] Update `DoclingConverter` to pass `input.source_meta.document_base_url or input.source_meta.source_url` as Docling's `source=` parameter

## Subprocess title selection + IPC

- [ ] Implement `select_source_title(document_title, resolver_title) -> str | None` with the UUID-shape and bare-URL rejection rules; place in `aizk.conversion.workers` (testable in isolation)
- [ ] Update `orchestrator._do_convert` body to: invoke `process_with_provenance`, run `select_source_title`, build a `SubprocessMetadata` instance, serialise via `model_dump_json` to `metadata.json`
- [ ] Update `orchestrator.process_with_provenance` to initialise an empty `SourceMetadata()` at the start of the fetch chain (so direct, non-resolver jobs receive a fully-`None` value), thread it through resolver hops via `merge()`, and produce a final merged `SourceMetadata` alongside the terminal `ConversionInput` and `ConversionArtifacts`.
  The empty value is also passed to terminal fetchers when no resolver runs.

## Parent process consumers

- [ ] Define a typed `SubprocessMetadataInvalid` exception (subclass of the existing conversion-error hierarchy) carrying `error_code = "subprocess_metadata_invalid"` and `retryable = False`, raised when `SubprocessMetadata.model_validate_json` fails (unknown extra field, missing required field, type mismatch); the orchestrator's failure handler maps `retryable=False` to a permanent job failure rather than the default-retryable path
- [ ] Update `_prepare_upload` to deserialise `metadata.json` into `SubprocessMetadata` (replacing `json.loads(read_text_nofollow(...))` + dict access); wrap `model_validate_json` to raise `SubprocessMetadataInvalid` on `ValidationError`
- [ ] Update `generate_manifest_v2` call in `_prepare_upload` to pass `source_url`, `source_normalized_url`, `source_title` from `SubprocessMetadata` (not from the `Source` row)
- [ ] Set `ConversionOutput.title = subprocess_meta.source_title or job.title` in both the content-hash-shortcut branch and the regular branch of `_prepare_upload`
- [ ] Extract Source-row enrichment into a separate function (e.g. `_write_source_enrichment(subprocess_meta, source_id, engine)`) called outside the manifest-building transaction; preserve existing best-effort failure semantics
- [ ] In `_write_source_enrichment`, populate all five fields from `SubprocessMetadata`: `Source.url ← source_meta.source_url`, `Source.normalized_url ← source_meta.normalized_url`, `Source.title ← source_title` (may be NULL), `Source.source_type` (existing), `Source.content_type` (existing)

## UI fix

- [ ] In `api/routes/ui.py` row construction (line ~138) flip precedence to `"title": source.title or job.title or ""`
- [ ] In `api/routes/ui.py` `_apply_filters` (line ~95) add `func.lower(Source.title).like(pattern)` to the `or_` group

## Tests

- [ ] Unit tests for `SourceMetadata.merge()`: empty + populated, two populated (earlier wins), all-`None` merges
- [ ] Unit tests for `select_source_title`: document title preferred, UUID-shaped rejected (cover both 32 and 36 char hex-and-dash variants), bare URL rejected (`http://`, `https://`), empty rejected, resolver fallback, both-`None` returns `None`
- [ ] Unit test for `SubprocessMetadata` round-trip: serialise → write `metadata.json` → read → deserialise; reject unknown extra fields; reject missing required fields
- [ ] Unit test for `KarakeepBookmarkResolver`: returns tuple; populates `source_url`, `document_base_url`, `resolver_title` from bookmark; `normalized_url` matches `normalize_url(source_url)`; for each of arxiv / github / pdf-asset / archive-asset / inline branches
- [ ] Unit test for `UrlFetcher` direct path: populates `source_url`, `document_base_url`, `normalized_url` from `ref.url`
- [ ] Unit test for `UrlFetcher` resolved path: preserves resolver-supplied `source_url` (does not overwrite with `ref.url` asset URL)
- [ ] Unit test for URL normalization edge cases: `source_url=None` → `normalized_url=None` and a debug log line is emitted; malformed `source_url` (e.g. raises from `normalize_url`) → `normalized_url=None` + debug log + no exception propagated; the worker job continues normally in both cases
- [ ] Unit test for `DoclingConverter`: emits raw `document_title` from a fixture HTML/PDF with a known title; emits `None` when source has no title; passes `document_base_url` to Docling
- [ ] Unit test for orchestrator: multi-hop resolver chain accumulates `SourceMetadata` correctly via `merge()`
- [ ] Integration test for the full pipeline: submit a Karakeep job, run worker end-to-end with stubbed external services, assert `Source.title`, `Source.url`, `Source.normalized_url`, manifest source block, and `ConversionOutput.title` all carry expected values
- [ ] Integration test: subprocess-side title selection produces correct `source_title` for all three branches (doc title, resolver fallback, both `None`)
- [ ] Integration test: Source enrichment failure (force a DB error on the enrichment write) does not affect manifest values or `ConversionOutput.title`
- [ ] Integration test for `/ui/jobs`: enriched `Source.title` displays in the title column; search by enriched title returns the row; placeholder displayed when `Source.title` is `NULL`
- [ ] Unit test for `SubprocessMetadataInvalid` mapping: when `metadata.json` contains an unknown extra field or a missing required field, `_prepare_upload` raises `SubprocessMetadataInvalid` and the orchestrator's failure handler records the job as `FAILED` with `error_code = "subprocess_metadata_invalid"` and `retryable = False` (no retry attempted)
- [ ] Update existing tests broken by signature changes: any test calling `fetcher.fetch(ref)` (one-arg) or treating resolver return as a single value; any test asserting `ConversionInput.metadata` is a dict
- [ ] Verify `ConversionOutput.title` insert succeeds when `subprocess_meta.source_title is None` (placeholder fallback covers the NOT NULL constraint)

## Cleanup / verification

- [ ] Remove `ConversionInput.metadata: dict[str, Any]` references anywhere they survive
- [ ] Remove the per-fetcher `source_url` lookup in `DoclingConverter` that was reading from the dict (`input.metadata.get("source_url")`)
- [ ] Run `ruff` / `mypy` / type-checker; resolve any drift around the new protocol signatures
- [ ] Run the conversion-worker test suite end-to-end against a real local stack (worker + API) with at least one Karakeep, one direct URL, one ArXiv, and one inline_html submission; spot-check `/ui/jobs` shows real titles
- [ ] Final verification: `uv run pytest tests/` passes with zero failures and no new warnings beyond the existing baseline
