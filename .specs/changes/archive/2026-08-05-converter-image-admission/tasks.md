# Tasks: Converter Image Admission

## Dependency bound

- [x] Change the `docling` constraint in `pyproject.toml` from `docling[easyocr,vlm]>=2.83` to `docling[easyocr,vlm]>=2.117.0,<2.118.0`
- [x] Refresh `uv.lock` against the new constraint and confirm the resolved `docling` version is unchanged at 2.117.0

## Pre-fetch admission — reference handling

Interface and pure helpers first; each is consumed by the outcome tasks in the next group.

- [x] Add a keyword-only `source_url: str | None = None` parameter to `prefetch_images` in `utilities/html_prefetch.py`
- [x] Add `_media_type_for_content_type` beside `_extension_for_content_type`, mapping the same fixed Content-Type allowlist to `image/*` media types and falling back to `image/octet-stream`
- [x] Unit-test `_media_type_for_content_type` for each allowlisted type, an unlisted type, a header carrying parameters (`; charset=…`), and an absent header
- [x] Skip any `<img>` whose `src` is empty, whitespace-only, or fragment-only before resolution or fetch
- [x] Case-fold the `data:` pass-through test so an uppercase `DATA:` URI takes the pass-through branch
- [x] Test that empty, whitespace-only, and fragment-only `src` values produce no outbound request and no resolution
- [x] Test that an uppercase `DATA:` URI passes through unchanged rather than being fetched or dropped
- [x] Resolve each remaining `src` against `source_url` with `urljoin`, placed ahead of the per-host cap's hostname keying
- [x] Test that a page-relative `src` is resolved against the source URL and evaluated by the egress policy as an ordinary outbound URL
- [x] Test that eleven page-relative `src` values on one document key to the source URL's host rather than to a single empty-host bucket, so the eleventh is not dropped by the per-host cap
- [x] Test that `source_url=None` leaves references unresolved and does not raise

## Pre-fetch admission — outcomes

Both contract assertions below have nine write-sites: the four cap arms (image count, total bytes, phase deadline, per-host), the four fetch-failure arms (`EgressPolicyError`, `FetchTooLargeError`, network `OSError`, unexpected `Exception`), and the workspace-write failure.
A tenth, the unparsable reference, emerged during implementation and is covered by its own task below.

- [x] On admission, set `src` to `data:<media-type>;base64,<bytes>` from the fetched bytes instead of the workspace path, retaining the workspace write
- [x] Test that an admitted image is carried as a `data:` URI in the returned HTML and that its workspace copy is still written
- [x] Delete the `src` attribute on every non-admission arm, replacing the current behaviour of leaving the original value in place
- [x] Parametrised test asserting `src` is absent from the returned HTML for each non-admission arm
- [x] Emit a per-reference record naming the reference and its non-admission class on each arm, keeping the existing once-per-document "cap reached" warning latch for the human-readable line
- [x] Parametrised test asserting a per-reference record carrying the correct non-admission class is emitted for each arm
- [x] Extend the end-of-phase summary so `skipped` accounts for cap-driven drops as well as the failure counters
- [x] Test that a document exceeding the image-count cap reports a `skipped` total equal to the number of references not admitted, rather than zero
- [x] Test that a deny-set `<img src>` drops the reference and records the rejection while `prefetch_images` returns normally rather than raising
- [x] Route a workspace-write failure to its own non-admission class, so the network errors that share `OSError` are not reported to operators as disk problems
- [x] Test that a failed workspace write records `disk_error` while a name-resolution failure records `network_error`
- [x] Drop one reference rather than the whole document when a reference cannot be parsed, so a malformed authority cannot fail the job
- [x] Test that an unparsable IPv6 authority drops only its own image, with and without a source URL, while the rest of the document is still admitted
- [x] Normalise the `data:` scheme on pass-through rather than only detecting it, since the converter matches the scheme case-sensitively
- [x] Test that a mixed-case `DATA:` reference survives admission in a form the converter decodes

## Converter configuration

Depends on the `prefetch_images` signature and the prefetch policy being available at converter-construction time.

- [x] Set `enable_local_fetch=False` and an explicit `render_page=False` on the `HTMLBackendOptions` built in `_create_document_converter`
- [x] Derive `max_image_data_base64_bytes` from the prefetch policy's `per_image_max_bytes` rather than leaving the library default, threading the policy into `_create_document_converter`
- [x] Drop the now-unused `workspace` parameter from `_create_document_converter` and its call sites
- [x] Pass `source_url` from `convert_html` into `prefetch_images`
- [x] Test that the constructed HTML backend options carry `enable_remote_fetch=False`, `enable_local_fetch=False`, `render_page=False`, and a base64 ceiling equal to the policy's `per_image_max_bytes`
- [x] Test that raising the policy's `per_image_max_bytes` above the library default raises the converter's base64 ceiling with it, so the two cannot disagree
- [x] Test that a page-authored `data:` image larger than `per_image_max_bytes` is rejected by the converter, closing the cap bypass

## Removal and documentation

- [x] Delete `src/aizk/conversion/utilities/docling_backend.py` and remove its import from `processing/converter.py`
- [x] Rewrite the `html_prefetch.py` module docstring, which currently describes rewriting `src` to an absolute local path and leaving failed references in place for a workspace-confinement gate to refuse
- [x] Replace the comment in `_create_document_converter` describing `local_fetch_root` and the confined subclass with the capability-removal rationale and the three upgrade-recheck facts from `design.md`
- [x] Update the workspace-confinement sentence in `src/aizk/conversion/README.md` that points at `docling_backend.py`, describing admission at the prefetch boundary instead
- [x] Correct the `TestEgressErrorPropagation` docstring in `tests/conversion/unit/processing/test_converter.py` that attributes `WorkspaceEscape` to "the confined Docling backend"

## Test-suite repair and gates

- [x] Repoint the two `requests` monkeypatches in `tests/conversion/unit/utilities/test_docling_remote_fetch_coverage.py` from `docling.backend.html_backend` to `docling.backend.utils.image_resource_loader`
- [x] Restore the multi-shape coverage test as the standing evidence for the converter's dereference surface, and extend its resource document with `<object data>` and `<embed src>`
- [x] Replace the three `make_confined_backend` tests with an end-to-end test that a hostile `<img src="/etc/ssh/ssh_host_rsa_key">` is stripped at admission, recorded, and never opened
- [x] Replace the vacuous `test_confined_backend_allows_workspace_local_image` with a test asserting an admitted image reaches `DoclingDocument.pictures` with a decoded image, not merely that conversion did not raise
- [x] Add a test that a document whose only images were dropped still converts, yielding figure placeholders and zero extracted figures
- [x] Add a test that a page-authored `data:` image is admitted with no outbound request and no filesystem read, and reaches `DoclingDocument.pictures` as a decoded image
- [x] Confirm the existing `TestEgressErrorPropagation` coverage still demonstrates that a rejection raised while dereferencing a source fails the job, now that the requirement distinguishes that arm from a rejection raised while admitting a referenced resource
- [x] Regenerate the conversion API OpenAPI snapshot into `schemas/after/` and confirm it is byte-identical to `schemas/before/`
- [x] Run `uv run pytest -n auto -m "not integration_lifecycle" tests/` and `uv run pytest -m integration_lifecycle tests/`
