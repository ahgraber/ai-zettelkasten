#!/usr/bin/env python3
"""Claimify evaluation driver.

Loads extraction JSONL for a handful of docs and runs the six evaluation
dimensions across a four-tier model congress via OpenRouter. Writes one
JSONL per doc under `data/claimify_demo/evaluation/` and renders a
per-model agreement table vs. per-unit baseline majority.

See:
- docs/superpowers/specs/2026-04-14-claimify-demo-design.md
- docs/superpowers/specs/2026-04-14-claimify-demo-plan.md (Milestone 10)
"""

# %%
from __future__ import annotations

import logging
import os
from pathlib import Path
import random

from _claimify.evaluation import (
    ALL_DIMENSIONS,
    agreement_table,
    evaluate_claims,
)
from _claimify.io import (
    EVALUATION_DIR,
    load_docs,
    read_evaluation_jsonl,
    read_extraction_jsonl,
)
from _claimify.models import ClaimRecord
from dotenv import load_dotenv
import nest_asyncio
from setproctitle import setproctitle

# %%
nest_asyncio.apply()
setproctitle(Path(__file__).stem)

logging.basicConfig(level=logging.INFO)
logging.getLogger("aizk").setLevel(logging.DEBUG)
logging.getLogger("_claimify").setLevel(logging.DEBUG)
logger = logging.getLogger(__name__)

_ = load_dotenv()
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")

# %%
# Same five bookmarks as the extraction driver. Must have an extraction JSONL
# on disk — re-run `claimify_extraction_demo.py` with RUN_FULL=True first.
KARAKEEP_IDS: list[str] = [
    "kbleumlsp93mtgx4r8dc6ext",  # Attention Is All You Need | arXiv PDF
    "w1aiidzcsie8ug40nx21q9ko",  # Illustrated Guide to OAuth | HTML w/ images
    "hojcn565u2m9smwtoehhjz3q",  # tinysearch | GitHub README
    "tufj0yp05tiqu485z4ocxs0u",  # OpenAI Sensitive Convos | Singlefile
    "mt2vc0ziqqt0pz6ptaqbf7yn",  # LLMs for Scientific Idea Generation | arXiv PDF
]

# %%
# Four-tier model congress. Fill with real OpenRouter IDs before running.
# See https://openrouter.ai/models — pick one model per price/capability band.
MODEL_TIERS: dict[str, list[str]] = {
    "frontier": ["anthropic/claude-opus-4-6", "openai/gpt-5"],
    "mid": ["anthropic/claude-sonnet-4-6", "openai/gpt-5-mini"],
    "baseline": ["anthropic/claude-haiku-4-5", "openai/gpt-5-nano"],
    "small": ["meta-llama/llama-3.3-70b-instruct", "google/gemini-2.5-flash"],
}

# Per-dimension adapter path overrides (default: "prose"). Tune per M5.5.
EVAL_PATHS: dict[str, str] = {
    # "coverage": "structured",
}

# %%
docs = load_docs(KARAKEEP_IDS)
doc_by_uuid = {d.aizk_uuid: d for d in docs}

extraction_by_doc: dict = {}
for d in docs:
    try:
        extraction_by_doc[d.aizk_uuid] = read_extraction_jsonl(d.aizk_uuid)
    except FileNotFoundError:
        print(f"!! missing extraction JSONL for {d.title} ({d.aizk_uuid}); skipping")

for uuid, recs in extraction_by_doc.items():
    n_claims = sum(1 for r in recs if isinstance(r, ClaimRecord))
    print(f"{doc_by_uuid[uuid].title[:60]:60s}  claims={n_claims}")

# %%
# Cost-guard: compute per-tier (calls × models) from the actual units.
# invalid_sentence/element/coverage: per sentence.
# entailment/decontextualization/invalid_claim: per (sentence, claim).
from collections import defaultdict  # noqa: E402

per_sentence = 0
per_sentence_claim = 0
for recs in extraction_by_doc.values():
    sentence_keys = set()
    claim_count = 0
    for r in recs:
        if isinstance(r, ClaimRecord):
            c = r.claim
            sentence_keys.add((c.section_idx, c.sentence_idx))
            claim_count += 1
    per_sentence += len(sentence_keys)
    per_sentence_claim += claim_count

dim_units = {
    "invalid_sentence": per_sentence,
    "element": per_sentence,
    "coverage": per_sentence,
    "entailment": per_sentence_claim,
    "decontextualization": per_sentence_claim,
    "invalid_claim": per_sentence_claim,
}
print(f"units per dimension: {dim_units}")
for tier_name, model_ids in MODEL_TIERS.items():
    total = sum(units * len(model_ids) for units in dim_units.values())
    print(f"tier={tier_name:<10s} models={len(model_ids)}  total calls={total}")
print("# Flip RUN_FULL=True below to proceed")

# %%
RUN_FULL = False


async def _run_eval(doc, records) -> Path:
    path = await evaluate_claims(
        doc,
        records,
        tiers=MODEL_TIERS,
        dimensions=ALL_DIMENSIONS,
        paths=EVAL_PATHS,
        api_key=OPENROUTER_API_KEY,
    )
    n = sum(1 for _ in read_evaluation_jsonl(doc.aizk_uuid))
    print(f"{doc.title[:60]:60s}  verdicts={n}  -> {path.name}")
    return path


if RUN_FULL:
    for d in docs:
        recs = extraction_by_doc.get(d.aizk_uuid)
        if not recs:
            continue
        await _run_eval(d, recs)

# %%
# Aggregation: load all eval verdicts, render per-model agreement table.
all_verdicts = []
for d in docs:
    p = EVALUATION_DIR / f"{d.aizk_uuid}.jsonl"
    if p.exists():
        all_verdicts.extend(read_evaluation_jsonl(d.aizk_uuid))

print(f"loaded {len(all_verdicts)} verdicts across {len(docs)} docs")

if all_verdicts:
    df = agreement_table(all_verdicts, MODEL_TIERS)
    try:
        from IPython.display import display

        display(df)
    except ImportError:
        print(df.to_string())

# %%
# Spot-check: pick a random (section, sentence, claim_idx) and print all
# dimensions × all models side by side.
by_key: dict[tuple, list] = defaultdict(list)
for rec in all_verdicts:
    by_key[(rec.section_idx, rec.sentence_idx, rec.claim_idx)].append(rec)

if by_key:
    key = random.choice(list(by_key.keys()))
    print(f"spot-check key section={key[0]} sentence={key[1]} claim={key[2]}")
    for rec in sorted(by_key[key], key=lambda r: (r.dimension, r.model)):
        print(f"  [{rec.dimension}] {rec.model}: {rec.result_json}")
