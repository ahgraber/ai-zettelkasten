# aizk.chunking

Document-structure splitter for converted Markdown artifacts.
Turns an already-normalized Markdown artifact into ordered, deterministically-identified structural chunks for downstream embedding and retrieval.

## Public surface

- `split(markdown_text, *, source_id, converted_artifact_id, markdown_hash_xx64, size_budget=DEFAULT_SIZE_BUDGET) -> list[Chunk]` — the deterministic, pure splitter entry point.
  No I/O; no dependence on wall-clock time, environment, or process identity.
- `Chunk` — the immutable chunk data model (identity, content hash, heading
  path, ordinal, text, span, and provenance fields).
- `SPLITTER_VERSION` — the splitter's behavior version; bumped whenever
  observable output changes for any input.
- `DEFAULT_SIZE_BUDGET` — the calibrated default per-chunk character budget.

## Behavior summary

A heading body within budget becomes one chunk.
An over-budget body is split on top-level block boundaries; a splittable paragraph that still exceeds the budget falls back to sentence-level splitting (`chonkie.SentenceChunker`).
A paragraph carrying an inline link, image, code span, or math span is kept whole instead of sentence-split, so the construct is never broken across chunks.
A non-splittable block (code, table, list, blockquote, math, raw HTML) is kept whole even when it exceeds the budget.
Frontmatter and pre-heading content are emitted under the empty heading path.

`chunk_id` is derived from the chunk's address `(source_id, heading_path, ordinal)` and its `content_hash`, so content edits and address moves are independently observable.
Identity is stable across processes.

## References

- Specification: [`.specs/specs/chunking/spec.md`](../../../.specs/specs/chunking/spec.md)
  (available after the change is synced into the baseline specs).
- Design decisions: [`docs/decision-record/005-chunking.md`](../../../docs/decision-record/005-chunking.md).
- Calibration record for `DEFAULT_SIZE_BUDGET`: `data/chunking-calibration/findings.md`.
