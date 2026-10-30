# Design: Docling Image Loader Confinement

## Context

Docling 2.143.0 handles `<img src>` in `HTMLDocumentBackend` as follows:

1. `_resolve_relative_path(loc)` delegates to `ImageResourceLoader.resolve_relative_path(loc, base_path)`, where `base_path` is `HTMLBackendOptions.source_uri`.
   With a remote `source_uri`, any `src` that does not start with `http://`, `https://`, `data:` or `#` is joined with `urljoin`, including absolute filesystem paths.
2. `_create_image_ref(src_url)` delegates to `ImageResourceLoader.create_image_ref(src_url, base_path)`.
   The loader fetches remote URLs only when `enable_remote_fetch` is set, through the module-level openers `_open_direct` and `_open_with_proxy`.
   It reads a local file only when `enable_local_fetch` is set and `base_path` is non-empty; relative paths are confined to the directory of a local `base_path`, and absolute paths are refused when `base_path` is local.
   `ValueError`, `OperationNotAllowed` and image-decoding errors become warnings and a missing image.

The converter runs with `enable_remote_fetch=False`, `enable_local_fetch=True`, and `source_uri` set to the bookmark URL (remote).
The image pre-fetch rewrites each `<img src>` to an absolute path inside the job workspace.

## Decisions

### Decision: OverrideResolveAndCreateImageRef

**Chosen:** The subclass returned by `make_confined_backend(workspace)` overrides two methods:

- `_resolve_relative_path(loc)`: if `loc` (with any `file://` prefix removed) is an absolute local path that resolves inside the workspace, return the resolved path; otherwise delegate to Docling.
  Root-relative links such as `/about` are not inside the workspace and still resolve against the origin.
- `_create_image_ref(src_url)`: remote URLs return `None` (logged); `data:` URIs delegate to Docling; every other location is resolved as a path and checked with `Path.is_relative_to(workspace)`.
  Outside paths raise `WorkspaceEscape`.
  Inside paths are read with `self._image_loader.create_image_ref(path, workspace / "document.html")`, so Docling's local-fetch switch and its size and pixel limits still apply; the synthetic base name makes the workspace the loader's confinement directory.

**Coupling:** both methods and the `_image_loader` attribute are private Docling API.
A Docling upgrade can rename them or move image loading again, which would bypass the overrides without an error.
`tests/conversion/unit/utilities/test_docling_remote_fetch_coverage.py` asserts that a prefetched workspace image is embedded and that escapes raise `WorkspaceEscape`, so such a change fails the suite.
The remote-fetch spike tests patch `docling.backend.utils.image_resource_loader._open_direct` and `_open_with_proxy`; if Docling moves its transport, the inverse spike test (remote fetch enabled must open a connection) fails.

**Alternatives considered:**

- Set `source_uri` to a local path inside the workspace and have the pre-fetch write relative `src` values, relying on Docling's own confinement: rejected; hyperlinks in the document would then resolve against the workspace instead of the origin site, and out-of-workspace paths would be dropped silently instead of raising `WorkspaceEscape`.
- Pin Docling below 2.110.0: rejected; the Docling advisories are fixed only in 2.131.0 and later.
- Override only `_create_image_ref`: rejected; by the time it runs, a remote `source_uri` has already turned the workspace path into a remote URL.

## Risks

- Relative `src` values with no `source_uri` resolve against the process working directory and raise `WorkspaceEscape`, matching the confined backend's behavior before this change.
- `data:` URIs bypass the path check; they carry inline content and are size-limited by Docling.
