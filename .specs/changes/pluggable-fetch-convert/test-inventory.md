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

- [x] `tests/conversion/unit/test_worker.py` — **rewrite-post-cutover** (rewritten in PR 7; passes against injected `WorkerRuntime`).
- [x] `tests/conversion/unit/test_worker_concurrency.py` — **rewrite-post-cutover** (updated in PR 7; `process_job_supervised` signature updated to accept `runtime`).
- [x] `tests/conversion/unit/test_worker_shutdown.py` — **rewrite-post-cutover** (rewritten in PR 7 against injected runtime; stale patches removed).
- [x] `tests/conversion/unit/test_error_tracebacks.py` — **rewrite-post-cutover** (passes unchanged; imports still valid via new orchestrator).
- [x] `tests/conversion/integration/test_worker_lifecycle.py` — **rewrite-post-cutover** (rewritten in PR 7; stale `fetch_karakeep_bookmark` patches removed, `source_ref` set on jobs, fake runtime passed).
- [x] `tests/conversion/integration/test_whitespace_normalization.py` — **rewrite-post-cutover** (rewritten in PR 7; uses `_process_job_subprocess` stub with workspace writes).
- [x] `tests/conversion/integration/test_conversion_flow.py` — **rewrite-post-cutover** (rewritten in PR 7; submits via `source_ref`, uses subprocess stub, new `compute_idempotency_key` signature).

## Notes

- The Stage 2 `tests/conversion/unit/core/*` suite already exercises the new
  `Orchestrator` contract with fakes and requires no movement.
- The Stage 1 `tests/conversion/unit/core/test_{types,source_ref,protocols,registry,errors}.py`
  suite stays in place permanently.
- Re-export shims added in PRs 3–4 keep **stay**-classified tests green without
  edits until PR 8 removes the shims; at that point, any residual imports of
  old module paths must be updated (tracked as PR 8's verification step).
