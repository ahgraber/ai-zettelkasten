# Tasks: Docling Image Loader Confinement

## 1. Confined Backend

- [x] Replace the `_load_image_data` override in `src/aizk/conversion/utilities/docling_backend.py` with `_resolve_relative_path` and `_create_image_ref` overrides per `design.md` § OverrideResolveAndCreateImageRef.
- [x] Document in the module and class docstrings the Docling mechanism the overrides depend on and their coupling to private Docling methods.
- [x] Update the HTML backend comment in `src/aizk/conversion/workers/converter.py`.

## 2. Tests

- [x] Add `test_confined_backend_loads_prefetched_workspace_image`, parametrized over no `source_uri` and a remote `source_uri`, asserting the picture carries image data.
- [x] Add `test_confined_backend_keeps_root_relative_links_on_source_origin`.
- [x] Keep `test_confined_backend_blocks_path_traversal` and `test_confined_backend_blocks_absolute_path_outside_workspace` asserting `WorkspaceEscape`.
- [x] Move the remote-fetch spike tests to Docling's connection openers (`_open_direct`, `_open_with_proxy`).
- [x] Run the confined-backend tests against the previous backend implementation and confirm the new tests fail.

## 3. Specs

- [ ] Sync the pluggable-pipeline delta into `.specs/specs/pluggable-pipeline/spec.md` and archive this change after merge.
