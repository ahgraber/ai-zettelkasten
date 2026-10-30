# Proposal: Docling Image Loader Confinement

## Intent

Docling's HTML backend loads `<img src>` through `ImageResourceLoader` (introduced between Docling 2.100.0 and 2.110.0).
The workspace-confined backend in `src/aizk/conversion/utilities/docling_backend.py` hooks a method that this path does not call, so with Docling 2.143.0:

- Images that the image pre-fetch copied into the job workspace are dropped from every converted HTML document.
  The converter passes the bookmark URL as `source_uri`, Docling joins the absolute workspace path onto it, and the result is refused as a remote fetch.
- Out-of-workspace paths are still not read (Docling refuses them), but the backend no longer raises `WorkspaceEscape`, so the job does not fail with the out-of-workspace classification the spec requires.

The Docling upgrade is required to clear published Docling advisories (fixed in 2.131.0 and 2.132.0), so pinning Docling below the loader change is not an option.

## Scope

**In scope:**

- Re-hook the confined backend onto Docling's current image path so workspace images load and out-of-workspace paths raise `WorkspaceEscape`.
- Strengthen the pluggable-pipeline egress requirement so a permitted workspace read must also embed the image, including when the document has a remote origin.
- Tests that assert a prefetched workspace image is embedded (with and without a remote origin), that escapes raise, that root-relative links still resolve to the origin, and that remote fetch is blocked at Docling's real connection openers.

**Out of scope:**

- Changes to the image pre-fetch (`html_prefetch.py`) or the egress gate.
- Replacing the subclass with a Docling-native option; Docling has no option to confine image reads to a caller-chosen directory.

## Approach

Override `_resolve_relative_path` and `_create_image_ref` on the confined subclass instead of `_load_image_data`.
See `design.md`.
