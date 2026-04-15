# Claimify demo

Two `#%%`-delimited Python notebooks that exercise a `pydantic-ai`
implementation of the Claimify extraction and evaluation pipelines on a
5-doc subset of the aizk corpus.

Design: [`docs/superpowers/specs/2026-04-14-claimify-demo-design.md`](../../docs/superpowers/specs/2026-04-14-claimify-demo-design.md)
Plan: [`docs/superpowers/specs/2026-04-14-claimify-demo-plan.md`](../../docs/superpowers/specs/2026-04-14-claimify-demo-plan.md)

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
