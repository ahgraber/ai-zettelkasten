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

import asyncio
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
    resolve_repo_root,
)
from _claimify.models import ClaimRecord
from _claimify.usage import summarize
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

REPO_ROOT = resolve_repo_root()
os.chdir(REPO_ROOT)

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
# Alternate candidates (from notebooks/docling_demo.py):
# "xt2omosp2erha7k4xd6mg9je",  # OpenAI ChatGPT Agent
# "rpnt3mzc96g5uhovbv2runu4",  # Sycophancy and the Pepsi Challenge
# "e8oks8mh930yfvcg2k0yzuvb",  # Treadmill 17 Jan 2025
# "qks067chkb8t1kprtm7rqbxl",  # OpenAI Confessions

# %%
# Four-tier model congress. Fill with real OpenRouter IDs before running.
# See https://openrouter.ai/models — pick one model per price/capability band.
MODEL_TIERS: dict[str, list[str]] = {
    # "frontier": ["anthropic/claude-sonnet-4-6", "openai/gpt-5.4"],
    # "mid": ["anthropic/claude-haiku-4.5", "openai/gpt-5.4-mini"],
    # "small": ["openai/gpt-5.4-nano", "google/gemini-3.1-flash-lite-preview"],
    "self-hostable": ["google/gemma-4-26b-a4b-it", "qwen/qwen3.5-9b"],
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
# invalid_sentence: per tokenized sentence (including sentences Selection dropped).
# element/coverage: per sentence WITH at least one claim.
# entailment/decontextualization/invalid_claim: per (sentence, claim).
from collections import defaultdict  # noqa: E402

from _claimify.pipeline import build_sentence_contexts  # noqa: E402
from _claimify.structuring import split_by_headings  # noqa: E402

total_sentences = 0
sentences_with_claims = 0
total_claims = 0
for doc_uuid, recs in extraction_by_doc.items():
    doc = doc_by_uuid[doc_uuid]
    for section_idx, section in enumerate(split_by_headings(doc.markdown)):
        total_sentences += len(build_sentence_contexts(section, section_idx, p=0, f=0))
    sentence_keys = set()
    for r in recs:
        if isinstance(r, ClaimRecord):
            c = r.claim
            sentence_keys.add((c.section_idx, c.sentence_idx))
            total_claims += 1
    sentences_with_claims += len(sentence_keys)

dim_units = {
    "invalid_sentence": total_sentences,
    "element": sentences_with_claims,
    "coverage": sentences_with_claims,
    "entailment": total_claims,
    "decontextualization": total_claims,
    "invalid_claim": total_claims,
}
print(f"units per dimension: {dim_units}")
for tier_name, model_ids in MODEL_TIERS.items():
    total = sum(units * len(model_ids) for units in dim_units.values())
    print(f"tier={tier_name:<10s} models={len(model_ids)}  total calls={total}")
print("# Flip RUN_FULL=True below to proceed")

# %%
# cost estimation: exact input token counts via tiktoken
# static parts (SYSTEM_PROMPT + USER_TEMPLATE scaffolding) tokenized once;
# variable parts summed over actual extraction records — no averaging.
import re  # noqa: E402

import tiktoken  # noqa: E402

from aizk.ai.claimify.prompts.evaluation import (  # noqa: E402
    coverage as _cov_p,
    decontextualization as _decon_p,
    element as _elem_p,
    entailment as _ent_p,
    invalid_claims as _ic_p,
    invalid_sentences as _is_p,
)

_enc = tiktoken.get_encoding("cl100k_base")


def _tok(s: str) -> int:
    return len(_enc.encode(s))


def _static_tokens(module) -> int:
    scaffolding = re.sub(r"\{\{[^}]+\}\}", "", module.USER_TEMPLATE)
    return _tok(module.SYSTEM_PROMPT) + _tok(scaffolding)


_STATIC_TOK = {
    "invalid_sentence": _static_tokens(_is_p),
    "element": _static_tokens(_elem_p),
    "coverage": _static_tokens(_cov_p),
    "entailment": _static_tokens(_ent_p),
    "decontextualization": _static_tokens(_decon_p),
    "invalid_claim": _static_tokens(_ic_p),
}

_Q_TOK = 20  # "What does section '<heading>' in '<title>' describe?"

# One pass over docs: build section tok table and sum invalid_sentence variable tokens.
# invalid_sentence fires for ALL sentences (including those Selection dropped, which
# aren't in the extraction records), so we iterate here rather than average.
_sec_tok: dict = {}  # (uuid, sec_idx) -> tokens
_is_var = 0  # variable token total for invalid_sentence
for _d in docs:
    for _si, _sec in enumerate(split_by_headings(_d.markdown)):
        _stok = _tok(_sec.content)
        _sec_tok[(_d.aizk_uuid, _si)] = _stok
        for _ctx in build_sentence_contexts(_sec, _si, p=0, f=0):
            _is_var += _Q_TOK + _stok + _tok(_ctx.sentence)

# One pass over extraction records: sentence tok per key, claims grouped by sentence.
# element / coverage fire per unique sentence-with-claims;
# entailment / decontextualization / invalid_claim fire per claim.
_sent_tok_key: dict = {}  # (uuid, sec_idx, sent_idx) -> tokens
_claims_by_sent: dict = {}
for _uuid, _recs in extraction_by_doc.items():
    for _r in _recs:
        if isinstance(_r, ClaimRecord):
            _c = _r.claim
            _k = (_uuid, _c.section_idx, _c.sentence_idx)
            _sent_tok_key.setdefault(_k, _tok(_c.sentence))
            _claims_by_sent.setdefault(_k, []).append(_c.claim.proposition)

_elem_cov_var = sum(_Q_TOK + _sec_tok[(_uuid, _si)] + _stok for (_uuid, _si, _), _stok in _sent_tok_key.items())

_ent_var = 0
_decon_var = 0
_ic_var = 0
for _uuid, _recs in extraction_by_doc.items():
    for _r in _recs:
        if isinstance(_r, ClaimRecord):
            _c = _r.claim
            _k = (_uuid, _c.section_idx, _c.sentence_idx)
            _base = _Q_TOK + _sec_tok[(_uuid, _c.section_idx)] + _sent_tok_key[_k]
            _ctok = _tok(_c.claim.proposition)
            _ent_var += _base + _ctok
            _decon_var += _base + sum(_tok(p) for p in _claims_by_sent[_k]) + _ctok
            _ic_var += _ctok

_VAR_TOK_TOTAL = {
    "invalid_sentence": _is_var,
    "element": _elem_cov_var,
    "coverage": _elem_cov_var,
    "entailment": _ent_var,
    "decontextualization": _decon_var,
    "invalid_claim": _ic_var,
}

_dim_order = ["invalid_sentence", "element", "coverage", "entailment", "decontextualization", "invalid_claim"]
print(f"static overhead (tokens): {_STATIC_TOK}")
print(f"\n{'dimension':<22s} {'units':>8s} {'total_input_tok':>16s}")
_grand_input_tok = 0.0
for _dim in _dim_order:
    _units = dim_units[_dim]
    _total_tok = _STATIC_TOK[_dim] * _units + _VAR_TOK_TOTAL[_dim]
    _grand_input_tok += _total_tok
    print(f"{_dim:<22s} {_units:>8d} {_total_tok:>16,.0f}")
print(f"\n{'TOTAL (1 model)':<31s} {_grand_input_tok:>16,.0f}")
for _tier_name, _model_ids in MODEL_TIERS.items():
    _tier_total = _grand_input_tok * len(_model_ids)
    print(f"tier={_tier_name:<12s} models={len(_model_ids)}  total input tokens = {_tier_total:>16,.0f}")


# %% [markdown]
# static overhead (tokens) from template: {
# 'invalid_sentence': 544,
# 'element': 1427,
# 'coverage': 2095,
# 'entailment': 2009,
# 'decontextualization': 760,
# 'invalid_claim': 232
# }
#
# | dimension           |  units | total_input_tok |
# |---------------------|-------:|----------------:|
# | invalid_sentence    |  2,547 |      16,386,224 |
# | element             |  2,388 |      17,597,843 |
# | coverage            |  2,388 |      19,193,027 |
# | entailment          | 10,715 |      97,328,808 |
# | decontextualization | 10,715 |      88,457,243 |
# | invalid_claim       | 10,715 |       2,905,239 |
#
# TOTAL (1 model)                      241,868,384
#
# Even with "self-hostable" prices ($0.08/$0.35 per 1M input/output tokens), _input-only_ costs are still $20; output costs might triple that.
#
# Ballpark $10-20 _per document_ just for evaluation... the existing 5k+ document corpus makes 'claimify' completely unaffordable.

# %%
CONCURRENCY = 6  # in-flight OpenRouter calls; matches bakeoff
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
    _sem = asyncio.Semaphore(CONCURRENCY)

    async def _one_doc(d):
        recs = extraction_by_doc.get(d.aizk_uuid)
        if not recs:
            return
        async with _sem:
            await _run_eval(d, recs)

    await asyncio.gather(*[_one_doc(d) for d in docs])

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

# %%
# Usage accounting per dimension (phase) and per model, plus overall evaluation.
# Each EvalRecord carries its own UsageSample for the LLM call that produced it.
usage_by_dim: dict[str, list] = {}
usage_by_model: dict[str, list] = {}
all_usage = []
for rec in all_verdicts:
    if rec.usage is None:
        continue
    all_usage.append(rec.usage)
    usage_by_dim.setdefault(rec.dimension, []).append(rec.usage)
    usage_by_model.setdefault(rec.model, []).append(rec.usage)

if all_usage:
    header = f"{'phase':<22s} {'calls':>6s} {'tot_tok':>10s} {'mean_tok':>10s} {'med_tok':>10s} {'cost_usd':>10s}"
    print("By dimension:")
    print(header)
    for dim in sorted(usage_by_dim):
        s = summarize(usage_by_dim[dim])
        cost = f"${s['total_cost_usd']:.4f}" if s["total_cost_usd"] is not None else "n/a"
        print(
            f"{dim:<22s} {s['calls']:>6d} {s['total_tokens']:>10d} "
            f"{s['mean_total_tokens']:>10.1f} {s['median_total_tokens']:>10.1f} {cost:>10s}"
        )
    overall = summarize(all_usage)
    overall_cost = f"${overall['total_cost_usd']:.4f}" if overall["total_cost_usd"] is not None else "n/a"
    print(
        f"{'OVERALL':<22s} {overall['calls']:>6d} {overall['total_tokens']:>10d} "
        f"{overall['mean_total_tokens']:>10.1f} {overall['median_total_tokens']:>10.1f} {overall_cost:>10s}"
    )

    print("\nBy model:")
    print(header.replace("phase", "model"))
    for m in sorted(usage_by_model):
        s = summarize(usage_by_model[m])
        cost = f"${s['total_cost_usd']:.4f}" if s["total_cost_usd"] is not None else "n/a"
        print(
            f"{m[:22]:<22s} {s['calls']:>6d} {s['total_tokens']:>10d} "
            f"{s['mean_total_tokens']:>10.1f} {s['median_total_tokens']:>10.1f} {cost:>10s}"
        )
else:
    print("No usage data on EvalRecords — re-run evaluation with the updated pipeline.")
