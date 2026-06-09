# Tasks: Contextualization Checkpoint Resume

> Ordered by build dependency: memo storage → repository helpers → generation-phase consumption → persist-phase prune → end-to-end behavior. Tests are paired with the unit they exercise. All behavioral tests drive the deterministic stub `LLMClient` and assert on its recorded invocations and on persisted records.

## Memo storage

- [x] Add the `ContextualizationOutputMemo` SQLModel (`graph/datamodel.py`, table `graph_contextualization_output_memo` — class unprefixed, matching `Chunk` / `DocumentSummary` / `ContextualizationJob`): `id`, `kind` (`summary` / `revision`), `scope_key` (`str(aizk_uuid)`), `derivation_key` (TEXT), `output_text` (TEXT, `''` legal), `created_at`; unique constraint `(kind, scope_key, derivation_key)`; index on the unique key.
- [x] Add an Alembic migration in `conversion/migrations/versions/` creating `graph_contextualization_output_memo` with the unique constraint and index; include `downgrade`.
- [x] Test: the migration upgrades and downgrades cleanly on a scratch database, and the unique constraint rejects a duplicate `(kind, scope_key, derivation_key)`.

## Memo repository helpers

- [x] Extract the `BEGIN IMMEDIATE` session context manager out of `workunit.py`'s private `_begin_immediate` into a shared low-level db helper (e.g. `graph/db.py` or `pipeline`), so the memo writer can open its own immediate transaction without `persistence.py` importing `workunit.py` internals (which would invert the workunit→persistence dependency direction); update `workunit.py` to use the shared helper.
- [x] Implement `memo_get(session, kind, scope_key, derivation_key) -> str | None` (`graph/persistence.py`): return the stored `output_text` (including `''`) on a hit, `None` on absence.
- [x] Implement `memo_upsert_and_read(engine, kind, scope_key, derivation_key, output_text) -> str` (`graph/persistence.py`): open its own short immediate transaction via the shared helper, `INSERT … ON CONFLICT(kind, scope_key, derivation_key) DO NOTHING`, then read and return the authoritative stored value.
- [x] Implement `memo_delete_keys(session, scope_key, keys: list[(kind, derivation_key)]) -> None`: delete exactly the listed keys under `scope_key`.
- [x] Test: `memo_get` distinguishes the three cases — absent → `None`, present-empty (`''`) → `''`, present-text → the text. (write-site: present-empty vs absent — requirement 2 / requirement 1 self-contained reuse)
- [x] Test: `memo_upsert_and_read` conflict path — pre-insert value `A` for a key, then call with value `B`; it returns `A` (the authoritative stored value), and the row is unchanged. (write-site: `ON CONFLICT DO NOTHING` winner semantics — requirement 1 contention safety)
- [x] Test: `memo_delete_keys` is key-exact — deletes only the listed keys for the source and leaves other same-`scope_key` keys intact. (write-site: key-exact prune — requirement 1 / requirement 3)
- [x] Test: the write transaction does not span a model call — with an instrumented stub `LLMClient` that asserts, on each invocation, that no memo write transaction is currently open (e.g. via a shared open-transaction flag the helper sets/clears), running the generation phase raises if any upsert transaction were held across a model call. (evidences the no-write-transaction-spans-a-model-call invariant)

## Generation-phase consumption

- [x] Refactor `_summary_identity` (`graph/contextualization.py`) to delegate to a new text-based `_summary_identity_from_text(summary_text, summary_version, summary_derivation_key)`, since the generation phase must compute the summary identity (for the variant run-level and per-row derivation keys) before any `DocumentSummary` row exists; the existing `_summary_identity(summary, key)` keeps its signature and calls the text-based helper, so persist-phase callers are unchanged.
- [x] Summary path: upgrade `resolve_summary_text` (`graph/contextualization.py`) to be memo-aware, and change its contract explicitly — it now takes the `engine` and performs autonomous memo writes (no longer read-only); rewrite its docstring accordingly.
  After the existing active-summary-run reuse check and before generating, consult `memo_get` for `(summary, scope_key, summary_derivation_key)`; on a hit return it with no model call; on a miss call the pure `generate_summary_text`, validate via `_validate_summary_text`, then `memo_upsert_and_read` and return the authoritative value.
  The pure `generate_summary_text` is unchanged.
- [x] Revision path: keep `generate_revisions` pure (model-I/O only, no DB) and add a memo-aware `resolve_revisions(engine, client, summary_text, summary_identity, ordered_chunks, …)`; extract a pure per-chunk generation helper (the 2p/1n framing + `client.generate`) that both `generate_revisions` and `resolve_revisions` use so a single chunk can be generated on a memo miss. `resolve_revisions` does the active-run precheck (below), then per chunk consults `memo_get` for `(revision, scope_key, _variant_row_derivation_key(...))`; on a hit (including `''`) uses it with no model call; on a miss generates the one chunk, validates via `_validate_contextualized_text`, then `memo_upsert_and_read` and uses the authoritative value.
- [x] Active variant-run precheck (inside `resolve_revisions`): before the per-chunk loop, build the run-level derivation key from the text-based summary identity and ordered chunks, and if a complete active variant run matches it, skip revision generation entirely (the persist phase reuses that run).
- [x] Move validation to the memo-write boundary: invalid output is not written to the memo and propagates as a failure; reuse the existing `_validate_*` functions (the persist-phase validation remains as a cheap idempotent re-check).
- [x] Wire `process_document` (`graph/workunit.py`) to call the upgraded `resolve_summary_text` and the new `resolve_revisions` with the `engine` so memo upserts commit autonomously, leaving the final `_begin_immediate` persist transaction unchanged in shape; `generate_revisions` is no longer called directly from the orchestration.
- [x] Test: a first-contextualization summary is written to the memo and, on a retry under unchanged inputs, is reused with zero summary model calls and a persisted summary equal to the first attempt's. (requirement 1 — summary reuse / scenario "summary reused across retry")
- [x] Test: a per-chunk revision is reused from the memo on retry with no model call for that chunk. (requirement 1 — revision reuse)
- [x] Test: the active variant-run precheck skips all revision generation when a complete active run matches — zero revision model calls. (write-site: active-run precheck — requirement 1 zero-invocation path, distinct from memo reuse)
- [x] Test: an output failing `_validate_contextualized_text` is not written to the memo, so a retry re-invokes the model for that chunk, and a subsequent valid output lets the unit succeed. (requirement 2 — invalid not retained; covers the revision validation write-site)
- [x] Test: a summary failing `_validate_summary_text` is not written to the memo and is re-invoked on retry. (requirement 2 — invalid not retained; covers the summary validation write-site)
- [x] Test: an empty (self-contained) revision is retained and reused on retry with no model call for that chunk, and persists as the empty variant on success. (requirement 2 — empty is valid and retained)

## Persist-phase prune

- [x] In the final `_begin_immediate` transaction (after `summarize_document` / `contextualize_chunks`), call `memo_delete_keys` for exactly the summary key and the per-chunk revision keys this generation consumed.
- [x] Test: after a successful generation, the consumed memo keys are gone while an unrelated same-`scope_key` memo row (a different derivation key) survives. (write-site: key-exact prune in the persist transaction — requirement 3 / requirement 1)

## End-to-end resume behavior

- [x] Test: a unit whose first attempt retains the summary and the first K of N revisions then fails re-invokes the model on retry only for the N−K remaining chunks — not the summary, not the first K — and on success persists one summary and one variant per chunk matching an uninterrupted run by count, provenance, and derivation-key linkage. (requirement 1 — primary scenario)
- [x] Test: re-executing an already-completed generation invokes the model zero times, creates no new run and no duplicate records, and reaches succeeded. (requirement 1 — completed re-execution; exercises both prechecks after the prune)
- [x] Test: two distinct sources with byte-identical Markdown — one with a retained summary from a partial attempt — do not share retained work; contextualizing the other invokes the model for its own summary. (requirement 1 — source isolation)
- [x] Test: a partial failure (no prior generation) leaves no contextualization run, summary, or variant active or readable for the source. (requirement 3 — nothing observable)
- [x] Test: a partial failure during re-contextualization under changed inputs leaves the prior completed generation's run, summary, and variants unchanged. (requirement 3 — prior generation undisturbed)
- [x] Test: incremental durability — when generation fails after K memo writes, exactly those K outputs are present in the memo (evidence that progress is checkpointed per output, not all-or-nothing).
