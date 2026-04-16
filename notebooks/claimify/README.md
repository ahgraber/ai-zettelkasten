# Claimify demo

## Status & scope

The demos are the reference implementation for a future production Claimify pipeline; if this graduates, the intended home is `src/aizk/extraction/` with `claimify` as a submodule owning its prompts.
The existing `src/aizk/ai/claimify/prompts/` package is touched only to import prompt strings.

### Goals

- Stand up runnable demos showing, end-to-end, how atomic claims are extracted from the aizk corpus and how their quality is evaluated across multiple model tiers.
- Serve as the reference implementation for the future production Claimify pipeline.

### Non-goals

- Production orchestration (no workers, DB schema changes, or API).
- Tabular / image / code claim extraction — stubbed with `TODO` placeholders that emit `SkippedArtifact` JSONL entries so downstream coverage can be reported honestly.
- Retrieval, embedding, or indexing of claims.
- Model / tier auto-selection.

## Architecture

### Extraction pipeline

`bookmark_id` → `conversion_outputs` join → `aizk_uuid` + S3 markdown key → cached S3 fetch → `MarkdownChef.process()` (prose separated from tables / code / images) → `structuring.split_by_headings()` → `list[Section]`.

Per section, in parallel branches:

- `contextualize.contextualize_section()` — one small-model LLM call producing a short `context_str` that situates the chunk in the full document.
  The full markdown lives in the **system prompt** so OpenRouter's prompt caching amortizes repeated calls; explicit `cache_control` ephemeral tag for Anthropic backends, implicit caching elsewhere.
  Sections run serially per doc to keep the cache warm.
- NLTK `sent_tokenize` — one-shot `punkt_tab` download guarded by a file-presence check.

For each sentence, build `(question, excerpt, sentence, preceding, following)` with paper-mirrored windows (`p=5, f=5` for selection; `p=5, f=0` for disambiguation and decomposition) and run:

```text
Agent[Selection] → SelectionResult (keep / drop / rewrite)
  → Agent[Disambiguation] → DisambigResult (resolve or fail)
    → Agent[Decomposition] → list[AtomicClaim]
      → extraction/<uuid>.jsonl
```

Sentences within a section fan out via `asyncio.gather` bounded by a semaphore (default 8).
Sections and docs are sequential.

### Evaluation pipeline

Consumes extraction JSONL.
Six evaluation agents — one per prompt — each with its own **call unit** (no uniform per-claim loop):

| Agent                 | Call unit             | Output        | Purpose            |
| --------------------- | --------------------- | ------------- | ------------------ |
| `invalid_sentences`   | per section           | list[bool]    | Selection errors   |
| `element`             | per sentence          | list[element] | Ground-truth units |
| `coverage`            | per sentence          | list[bool]    | Completeness       |
| `entailment`          | per (sentence, claim) | bool          | Faithfulness       |
| `decontextualization` | per (sentence, claim) | C_max text    | Self-containedness |
| `invalid_claims`      | per sentence          | list[bool]    | Filter failures    |

Each call unit fans out across four model tiers (`baseline`, `middle`, `small`, `tiny`) routed through OpenRouter.
Aggregation happens in the notebook so the raw verdict stream is preserved: per-tier majority vote, then cross-tier agreement against the baseline — Cohen's κ for categorical dimensions, exact + normalized match rate for textual `C_max`.

### Key design decisions

- **`notebooks/claimify/` is a `uv` workspace member** (`claimify-demo`) shipping an internal `_claimify/` helper package.
  The leading underscore signals "promote deliberately."
  Drivers stay thin; heavier logic (agents, doc loading, tier wiring) lives in `.py` modules per `python-notebooks-async`.
- **DIY header splitter** (~40 LOC, ATX-only, fence-aware).
  Chonkie's `RecursiveChunker` enforces a token bound we don't want — we want logical sections.
- **Discriminated-union JSONL records** (`ClaimRecord` / `SkippedRecord` / `FailedRecord` for extraction; `EvalRecord` for evaluation) so streams stay self-describing and LLM errors don't abort the run.
- **Adapter layer** between agents and pydantic result models; the structured-outputs-vs-prose-parser choice is made per-dimension during implementation (M5.5).
- **Cost guard:** each notebook prints a projected token + call estimate behind a `RUN_FULL = False` sentinel before the expensive pass.
- **Validation is three hermetic unit tests** at fragile non-LLM boundaries (header splitter, adapter parsing, `resolve_doc` cache/ordering).
  No integration or LLM-dependent tests at prototype stage.

### Open risks

- **Model ID drift** — tier placeholders need current OpenRouter IDs; unreachable models are logged and skipped (don't abort the tier).
- **Prompt-cache semantics vary by backend** — correctness unaffected, only cost rises if caching breaks.
- **Adapter path choice** — if both structured outputs and the prose parser degrade, the demo still runs with a best-effort parser and surfaces `AdapterParseError` counts.
- **Docling markdown variability** across `html` vs `pdf` pipelines — spot-check splitter output on at least one of each when running.

## Layout

- `_claimify/` — library package (pipeline, evaluation, adapters, io, models).
- `claimify_extraction_demo.py` — extraction driver (contextualize → select → disambiguate → decompose).
- `claimify_evaluation_demo.py` — evaluation driver (six dimensions × four-tier model congress).
- `tests/` — hermetic pytest suite; no network, no real OpenRouter calls.

## Install

This directory is a `uv` workspace member, so a plain `uv sync` at the repo root does **not** make `_claimify` importable.
Use one of:

```bash
# install the workspace member alongside the root project
uv sync --all-packages

# or install only the claimify demo
uv sync --package claimify-demo
```

The demo drivers additionally read `OPENROUTER_API_KEY` from `.env`.

## Run the tests

The claimify tests live outside the repo-root `testpaths` glob (notebooks are excluded from the main pytest discovery), so the standard `uv run pytest tests/` does not exercise them.
Run them explicitly:

```bash
uv run --package claimify-demo pytest notebooks/claimify/tests/ -q
```

The suite is hermetic: NLTK data download and `sent_tokenize` are
monkeypatched, the evaluation orchestrator runs against stubbed bundles,
and no network or OpenRouter calls are made.

## Run the notebooks

Both drivers support cell-by-cell execution (VS Code's Python cells, Jupytext, or `jupytext --to notebook`).
Each guards the expensive LLM pass behind a `RUN_FULL = False` flag — the cost-guard cell prints the projected call count per tier so you can sanity-check before flipping it.

Extraction runs first and writes one JSONL per doc to `data/claimify_demo/extraction/<aizk_uuid>.jsonl`.
The evaluation driver reads those JSONLs and writes verdicts to `data/claimify_demo/evaluation/<aizk_uuid>.jsonl`.
