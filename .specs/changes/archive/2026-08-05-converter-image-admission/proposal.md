# Proposal: Converter Image Admission

## Intent

Workspace confinement for converter image reads is enforced inside the converter, by subclassing the document-conversion library's HTML backend and overriding the method that loads image bytes.
That library relocated image loading into a shared resource loader, leaving the overridden method as an uncalled shim — so the control no longer runs, for the second time in this component's life.
The same relocation also made the library refuse every image the pipeline pre-fetches into the job workspace: it now resolves references against a declared base path, so the absolute workspace path the pipeline writes is joined onto the page's origin host and then refused as a remote fetch that the converter is configured not to perform.
The refusal surfaces as a Python warning, so HTML conversions silently produce no figures at all.
Owning confinement inside the converter has proven to be a control anchored to another project's internals, and this change moves image admission to the boundary this project owns.

## User Stories

### Story: figures-survive-ingestion

As an owner of the knowledge store, I want images from an archived web page to survive conversion into that source's artifacts, so that a captured resource keeps the visual evidence its text refers to.

### Story: no-silent-content-loss

As an operator self-hosting the pipeline, I want every image reference the pipeline refuses to admit to be recorded with the reason, so that I can tell a page that had no images from a page whose images were dropped.

### Story: hostile-page-cannot-read-local-files

As an owner ingesting untrusted archived pages, I want conversion to be structurally incapable of reading a local file named by page content, so that a hostile page cannot pull host data into my notes.

## Scope

**In scope:**

- Image admission at the pre-fetch boundary: resolve every `<img src>` against the source URL, admit only references that pass egress validation and the per-document caps, and strip every reference that is not admitted before the document reaches the converter.
- Passing admitted image bytes to the converter inline, so conversion performs no local filesystem read derived from page content.
- Recording every non-admission individually, including the four cap-driven ones that currently log once per document and are excluded from the phase summary.
- Configuring the converter so it can dereference no location at all, and stating that configuration as part of the contract rather than as an unstated backstop.
- Removing the workspace-confined converter backend subclass (`src/aizk/conversion/utilities/docling_backend.py`) and its wiring, including the reference to it in `src/aizk/conversion/README.md`.
- Test coverage for each admission outcome, replacing the tests that asserted the removed subclass's behavior, and repairing the existing multi-shape coverage test that is the standing evidence for the converter's dereference surface.
- Bounding the declared `docling` version constraint from above as well as below, as an operator preference for deliberate upgrades.
  This is a dependency-declaration change carried by this change, not a contract this codebase asserts; it is recorded in `design.md` and `tasks.md` and serves no requirement.

**Out of scope:**

- Changing the per-document pre-fetch cap values (image count, total bytes, per-image bytes, per-host count, phase deadline).
- The PDF conversion path — it performs no page-directed resource dereference.
- `WorkspaceEscape` and the containment checks in `utilities/paths.py` and manifest filename validation, which guard artifact paths rather than page-directed reads and are unaffected.
- Version-bounding the conversion library's transitive dependencies; the lockfile already pins those exactly.
- Backfilling previously converted sources.
  The committed lockfile still resolves the prior library version, so no conversion has run against the broken path.

## Approach

Pre-fetch already downloads each `<img src>` through the egress-validated helper and writes it into the job workspace.
Three changes to that phase carry the contract:

1. Resolve `<img src>` against the source URL, ahead of the per-host cap that keys on the reference's hostname, so page-relative references become ordinary outbound URLs the egress policy can evaluate.
   Today they are neither resolved nor fetchable, so they are skipped silently.
2. On admission, rewrite `src` to an inline `data:` URI carrying the fetched bytes, instead of to a workspace path.
   Continue writing the file into the workspace for replay.
3. On any non-admission — egress rejection, size cap, host cap, count cap, deadline, disk error — remove the `src` attribute rather than leaving the original in place, and record the reference with the class that caused the drop.
   The converter then emits a figure placeholder and performs no I/O for that reference.

The converter is then configured so that it can dereference nothing at all — no remote fetch, no local fetch, no browser render — and the confined-backend subclass is deleted.
Admission reaches only `<img src>`, which is the shape it has to rewrite anyway; every other resource-bearing shape a page might use is covered by the converter having no dereference capability rather than by stripping.
That split is deliberate: enumerating resource-bearing HTML attributes is a deny-list against an open set, while removing the capability is complete by construction.
The source URL stays as the converter's declared base path, which keeps hyperlink resolution against the page origin working.

An alternative shape — hand the converter a workspace-local base path and let its own confinement land on our workspace — was measured against the installed library and rejected: with a local base path, resolution of an absolute `<a href>` raises and aborts the whole conversion, and page hyperlinks stop resolving against their origin.
Rationale is recorded in `design.md`.

The declared `docling` constraint becomes `>=2.117.0,<2.118.0`, narrow enough that a minor-version move requires editing it by hand.

## Schema Impact

None.
This change is confined to the conversion worker's document-preparation and converter-configuration paths.
No API route, request model, or response model is added, removed, or modified, so `conversion-api-openapi.json` is expected to be byte-identical before and after.
