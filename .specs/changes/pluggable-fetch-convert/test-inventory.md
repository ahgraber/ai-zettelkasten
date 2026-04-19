# Conversion Test Inventory

Classification of every test module that imports the legacy modules being extracted into `aizk.conversion.adapters` (`converter.py`, `fetcher.py`, `bookmark_utils.py`, `arxiv_utils.py`, `github_utils.py`, or the legacy worker `orchestrator.py`).
Used as a tracking checklist across PRs 3–7.

Classification key:

- **stay**: keeps current location — imports the legacy re-export path, no move
  required (legacy path remains valid until PR 8 removes re-exports).
- **move-with-adapter**: relocates alongside the adapter module in PR 4/7, because
  the test exercises fetcher/resolver-specific behavior.
- **rewrite-post-cutover**: test is coupled to pre-refactor orchestration
  internals (worker orchestrator loop, supervision, old SourceRef-less job
  schema); rewritten or retired in PR 7 (worker cutover).

## Inventory — converter.py (DoclingConverter extraction, PR 3)

- [ ] `tests/conversion/unit/test_converter_tracing.py` — **stay** (re-export keeps import path valid; test exercises tracing attributes on the Docling adapter module, not orchestration).
- [ ] `tests/conversion/unit/test_converter_classification.py` — **stay** (re-export keeps import path valid; test targets picture-classification enrichment inside the Docling adapter).
- [ ] `tests/conversion/unit/test_worker.py` — **rewrite-post-cutover** (deep worker-orchestration integration; rewrites in PR 7 against the injected `Orchestrator`).

## Inventory — fetcher.py / bookmark_utils.py / arxiv_utils.py / github_utils.py (PR 4)

- [x] `tests/conversion/unit/test_fetcher.py` → `tests/conversion/unit/adapters/fetchers/test_fetcher.py` — **moved** (PR 4).
- [x] `tests/conversion/unit/test_bookmark_utils.py` → `tests/conversion/unit/adapters/fetchers/test_bookmark_utils.py` — **moved** (PR 4).
- [x] `tests/conversion/unit/test_url_utils.py` → `tests/conversion/unit/adapters/fetchers/test_url_utils.py` — **moved** (PR 4).
- [x] `tests/conversion/unit/test_arxiv_utils.py` → `tests/conversion/unit/adapters/fetchers/test_arxiv_utils.py` — **moved** (PR 4).
- [x] `tests/conversion/unit/test_github_utils.py` → `tests/conversion/unit/adapters/fetchers/test_github_utils.py` — **moved** (PR 4).

## Inventory — workers/orchestrator.py (legacy orchestrator, PR 7)

- [ ] `tests/conversion/unit/test_worker.py` — **rewrite-post-cutover** (see above).
- [ ] `tests/conversion/unit/test_worker_concurrency.py` — **rewrite-post-cutover** (GPU-semaphore + loop concurrency assumptions change with injected `ResourceGuard`).
- [ ] `tests/conversion/unit/test_worker_shutdown.py` — **rewrite-post-cutover** (shutdown coupling to legacy orchestrator shape; rewrite against injected runtime in PR 7).
- [ ] `tests/conversion/unit/test_error_tracebacks.py` — **rewrite-post-cutover** (imports `workers.orchestrator`; re-targets at new orchestrator in PR 7).
- [ ] `tests/conversion/integration/test_worker_lifecycle.py` — **rewrite-post-cutover** (full worker loop; retargets at `build_worker_runtime(cfg)` in PR 7).
- [ ] `tests/conversion/integration/test_whitespace_normalization.py` — **rewrite-post-cutover** (integrates `workers.orchestrator` with uploader; retargets in PR 7).
- [ ] `tests/conversion/integration/test_conversion_flow.py` — **rewrite-post-cutover** (end-to-end integration; deferred test in PR 7 per tasks.md).

## Notes

- The Stage 2 `tests/conversion/unit/core/*` suite already exercises the new
  `Orchestrator` contract with fakes and requires no movement.
- The Stage 1 `tests/conversion/unit/core/test_{types,source_ref,protocols,registry,errors}.py`
  suite stays in place permanently.
- Re-export shims added in PRs 3–4 keep **stay**-classified tests green without
  edits until PR 8 removes the shims; at that point, any residual imports of
  old module paths must be updated (tracked as PR 8's verification step).
