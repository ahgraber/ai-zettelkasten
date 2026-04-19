#!/usr/bin/env python3
"""Claimify adapter bakeoff (M5.5).

For each extraction stage (selection / disambiguation / decomposition) and
each evaluation dimension (invalid_sentence / element / coverage / entailment
/ decontextualization / invalid_claim), run BOTH adapter paths — "prose" and
"structured" — over a fixed sample and tabulate:

    calls | parse_errors | mean_latency_s | total_tokens | total_cost | agreement

`agreement` compares the two paths' outputs on the same input (per-unit
equality for bools / Jaccard for lists / normalized-text match for strings).
The winner per stage/dimension seeds the defaults in
`claimify_extraction_demo.py` and `claimify_evaluation_demo.py`.

See:
- docs/superpowers/specs/2026-04-14-claimify-demo-design.md
- docs/superpowers/specs/2026-04-14-claimify-demo-plan.md (Milestone 5.5)
"""

# %%
from __future__ import annotations

import asyncio
from collections import defaultdict
import logging
import os
from pathlib import Path
import subprocess  # noqa: E402
import time

from _claimify.adapters import AdapterParseError
from _claimify.evaluation import bundle_for
from _claimify.io import (
    DATA_DIR,
    ensure_punkt_tab,
    load_docs,
    resolve_repo_root,
)
from _claimify.pipeline import (
    build_sentence_contexts,
    extraction_question,
    make_decomposition_runner,
    make_disambiguation_runner,
    make_selection_runner,
)
from _claimify.structuring import split_by_headings
from dotenv import load_dotenv
import nest_asyncio
from setproctitle import setproctitle

from aizk.conversion.utilities.config import ConversionConfig  # noqa: E402
from aizk.conversion.utilities.litestream import (  # noqa: E402
    _litestream_env,
    _resolve_litestream_binary,
    _write_config_file,
)

# %%
nest_asyncio.apply()
setproctitle(Path(__file__).stem)

logging.basicConfig(level=logging.INFO)
logging.getLogger("aizk").setLevel(logging.INFO)
logging.getLogger("_claimify").setLevel(logging.INFO)
logger = logging.getLogger(__name__)

REPO_ROOT = resolve_repo_root()
os.chdir(REPO_ROOT)

_ = load_dotenv()
OPENROUTER_API_KEY = os.environ.get("_OPENROUTER_API_KEY") or os.environ.get("OPENROUTER_API_KEY")

BAKEOFF_DIR = DATA_DIR / "bakeoff"
BAKEOFF_DIR.mkdir(parents=True, exist_ok=True)

# %%
# Restore the SQLite DB from Litestream (same dance as the extraction demo).
_config = ConversionConfig()
_db_path = (REPO_ROOT / "data" / "conversion_service.db").resolve()
for _suffix in ("", "-wal", "-shm"):
    (_db_path.parent / f"{_db_path.name}{_suffix}").unlink(missing_ok=True)

_bucket = _config.litestream_s3_bucket_name or _config.s3_bucket_name
_ls_config_path = _write_config_file(
    db_path=_db_path,
    bucket=_bucket,
    config_path=Path(_config.litestream_config_path),
    s3_prefix=_config.litestream_s3_prefix,
    s3_region=_config.s3_region,
    s3_endpoint_url=_config.s3_endpoint_url,
    s3_force_path_style=_config.litestream_s3_force_path_style,
    s3_sign_payload=_config.litestream_s3_sign_payload,
)
_ls_binary = _resolve_litestream_binary(_config.litestream_binary)
subprocess.run(
    [_ls_binary, "restore", "-config", str(_ls_config_path), str(_db_path)],
    check=True,
    env=_litestream_env(_config),
)

# %%
# One doc, one section — keeps the bill small while still exercising every
# stage/dimension on real prose. Swap in a longer doc if the sample is too
# uniform to separate the adapters.
#
# The bakeoff is self-contained: extraction-stage calls are what produce the
# claim pool that the per-claim eval dimensions then score. No reliance on a
# prior `claimify_extraction_demo.py` run.
KARAKEEP_ID = "rpnt3mzc96g5uhovbv2runu4"  # Sycophancy and the Pepsi Challenge
SECTION_IDX = 0  # index into split_by_headings(doc.markdown)
MAX_SENTENCES = 12  # cap per stage to bound cost
MAX_CLAIMS = 8  # cap per-claim dimensions; harvested from the stage bakeoff

# One model per phase — we're measuring the adapter, not the model. Pick a
# mid-tier model that's cheap enough to run twice per unit.
STAGE_MODEL = "openai/gpt-5-mini"
EVAL_MODEL = "openai/gpt-5-mini"

# %%
docs = load_docs([KARAKEEP_ID])
doc = docs[0]
sections = split_by_headings(doc.markdown)
section = sections[SECTION_IDX]
print(f"doc={doc.title!r} section='{'/'.join(section.heading_path) or '<lead>'}'")
print(f"sections_total={len(sections)} section_chars={len(section.content)}")

ensure_punkt_tab()
contexts = build_sentence_contexts(section, SECTION_IDX, p=5, f=5)[:MAX_SENTENCES]
question = extraction_question(doc, section, context_str="(no contextualizer — bakeoff)")
print(f"sentences_under_test={len(contexts)}")

# %%
# Cost guard. Extraction stages: MAX_SENTENCES × 2 paths × 3 stages. For the
# eval upper bound assume every sentence yields ~2 claims (we'll cap to
# MAX_CLAIMS after the stage bakeoff actually runs). Eval dims:
#   sentences × 2 paths × (invalid_sentence, element)
# + claims    × 2 paths × (entailment, decontextualization, invalid_claim)
est_stage_calls = len(contexts) * 2 * 3
est_claim_count = min(MAX_CLAIMS, len(contexts) * 2)
est_eval_calls = len(contexts) * 2 * 2 + est_claim_count * 2 * 3
print(f"estimated calls: stages={est_stage_calls}  eval<={est_eval_calls}  total<={est_stage_calls + est_eval_calls}")
print("# Flip RUN_FULL=True below to proceed")

# %%
RUN_FULL = False


# %%
# Results shape: list of dicts, one per (phase, path, unit_idx) call.
async def _timed(coro):
    t0 = time.perf_counter()
    try:
        out = await coro
        return out, None, time.perf_counter() - t0
    except AdapterParseError as e:
        return None, f"parse:{e}", time.perf_counter() - t0
    except Exception as e:  # noqa: BLE001 — we want every failure class tabulated
        return None, f"{type(e).__name__}:{e}", time.perf_counter() - t0


CONCURRENCY = 6  # in-flight OpenRouter calls; leaves headroom for rate limits


async def _bake_stage(phase: str, runner_factory, *, include_following: bool) -> list[dict]:
    """Fan out both paths × all sentences concurrently under CONCURRENCY slots."""
    sem = asyncio.Semaphore(CONCURRENCY)
    runners = {path: runner_factory(path) for path in ("prose", "structured")}

    async def _one(path: str, i: int, ctx) -> dict:
        async with sem:
            out, err, dt = await _timed(runners[path](ctx, question))
        result, usage = out if out is not None else (None, None)
        return {
            "phase": phase,
            "path": path,
            "unit": i,
            "ok": err is None,
            "error": err,
            "latency_s": dt,
            "tokens": usage.total_tokens if usage else None,
            "cost_usd": usage.cost_usd if usage else None,
            "result": result,
            "sentence": ctx.sentence,
        }

    tasks = [_one(path, i, ctx) for path in ("prose", "structured") for i, ctx in enumerate(contexts)]
    return list(await asyncio.gather(*tasks))


def _selection_factory(path):
    return make_selection_runner(STAGE_MODEL, path=path, api_key=OPENROUTER_API_KEY)


def _disambig_factory(path):
    return make_disambiguation_runner(STAGE_MODEL, path=path, api_key=OPENROUTER_API_KEY)


def _decomp_factory(path):
    return make_decomposition_runner(STAGE_MODEL, path=path, api_key=OPENROUTER_API_KEY)


stage_rows: list[dict] = []
if RUN_FULL:
    stage_rows += await _bake_stage("selection", _selection_factory, include_following=True)
    stage_rows += await _bake_stage("disambiguation", _disambig_factory, include_following=False)
    stage_rows += await _bake_stage("decomposition", _decomp_factory, include_following=False)
    print(f"stage rows: {len(stage_rows)}")


# %%
# Harvest the claim pool the eval bakeoff will score. Prefer structured
# decomposition results when both paths succeeded on the same unit (arbitrary
# tie-break — we're not judging the claims, just needing *some* real claims
# produced by the pipeline under test). Capped at MAX_CLAIMS.
def _harvest_claims(rows: list[dict]) -> list[tuple[str, str]]:
    by_unit: dict[int, dict[str, dict]] = defaultdict(dict)
    for r in rows:
        if r["phase"] == "decomposition" and r["ok"] and r["result"] is not None:
            by_unit[r["unit"]][r["path"]] = r
    pool: list[tuple[str, str]] = []
    for unit in sorted(by_unit):
        pick = by_unit[unit].get("structured") or by_unit[unit].get("prose")
        if pick is None:
            continue
        for c in pick["result"].claims:
            pool.append((pick["sentence"], c.proposition))
    return pool[:MAX_CLAIMS]


claim_pool: list[tuple[str, str]] = _harvest_claims(stage_rows) if stage_rows else []
print(f"claim_pool_size={len(claim_pool)}")


# %%
# Evaluation bakeoff. Reuses `bundle_for` so we get the same runners the
# evaluation demo uses. We call each runner directly rather than through
# `evaluate_claims` so per-path / per-call metrics stay clean.
async def _bake_eval() -> list[dict]:
    """Fan out all (path × sentence/claim × dimension) calls under CONCURRENCY slots."""
    sem = asyncio.Semaphore(CONCURRENCY)
    excerpt = section.content
    sentences = [(i, c.sentence) for i, c in enumerate(contexts)]
    claim_texts = [prop for _sent, prop in claim_pool]

    # Build one bundle per path (each has its own agent/client instances).
    bundles = {
        path: bundle_for(
            EVAL_MODEL,
            paths=dict.fromkeys(
                ("invalid_sentence", "element", "coverage", "entailment", "decontextualization", "invalid_claim"),
                path,
            ),
            api_key=OPENROUTER_API_KEY,
        )
        for path in ("prose", "structured")
    }

    async def _sent(path: str, i: int, sent: str, dim: str, coro) -> dict:
        async with sem:
            out, err, dt = await _timed(coro)
        result, usage = out if out is not None else (None, None)
        return {
            "phase": dim,
            "path": path,
            "unit": i,
            "ok": err is None,
            "error": err,
            "latency_s": dt,
            "tokens": usage.total_tokens if usage else None,
            "cost_usd": usage.cost_usd if usage else None,
            "result": result,
            "sentence": sent,
        }

    async def _claim(path: str, j: int, sent: str, claim_text: str, dim: str, coro) -> dict:
        async with sem:
            out, err, dt = await _timed(coro)
        result, usage = out if out is not None else (None, None)
        return {
            "phase": dim,
            "path": path,
            "unit": j,
            "ok": err is None,
            "error": err,
            "latency_s": dt,
            "tokens": usage.total_tokens if usage else None,
            "cost_usd": usage.cost_usd if usage else None,
            "result": result,
            "claim": claim_text,
        }

    tasks = []
    for path, bundle in bundles.items():
        for i, sent in sentences:
            tasks.append(_sent(path, i, sent, "invalid_sentence", bundle.invalid_sentence(question, excerpt, sent)))
            tasks.append(_sent(path, i, sent, "element", bundle.element(question, excerpt, sent)))
        for j, (sent, claim_text) in enumerate(claim_pool):
            tasks.append(
                _claim(path, j, sent, claim_text, "entailment", bundle.entailment(question, excerpt, sent, claim_text))
            )
            tasks.append(
                _claim(
                    path,
                    j,
                    sent,
                    claim_text,
                    "decontextualization",
                    bundle.decontextualization(question, excerpt, sent, claim_texts, claim_text),
                )
            )
            tasks.append(_claim(path, j, sent, claim_text, "invalid_claim", bundle.invalid_claim(claim_text)))

    return list(await asyncio.gather(*tasks))


eval_rows: list[dict] = []
if RUN_FULL:
    eval_rows = await _bake_eval()
    print(f"eval rows: {len(eval_rows)}")


# %%
# Per-phase-per-path aggregation.
def _summarize(rows: list[dict]) -> None:
    if not rows:
        print("(no rows — set RUN_FULL=True)")
        return
    buckets: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        buckets[(r["phase"], r["path"])].append(r)

    header = f"{'phase':<22s} {'path':<11s} {'calls':>6s} {'errs':>5s} {'mean_s':>8s} {'tokens':>10s} {'cost':>8s}"
    print(header)
    for (phase, path), items in sorted(buckets.items()):
        n = len(items)
        errs = sum(1 for r in items if not r["ok"])
        lat = sum(r["latency_s"] for r in items) / n
        toks = sum(r["tokens"] or 0 for r in items)
        costs = [r["cost_usd"] for r in items if r["cost_usd"] is not None]
        cost = f"${sum(costs):.4f}" if costs else "n/a"
        print(f"{phase:<22s} {path:<11s} {n:>6d} {errs:>5d} {lat:>8.2f} {toks:>10d} {cost:>8s}")


print("=== extraction stages ===")
_summarize(stage_rows)
print("\n=== evaluation dimensions ===")
_summarize(eval_rows)

# %%
# Agreement between prose and structured on the same unit. For each phase,
# pair up (path=prose, unit=i) with (path=structured, unit=i) where both
# succeeded and compute a dimension-appropriate match score.


def _normalize(s: str) -> str:
    return s.strip().lower().rstrip(".!?;:")


def _jaccard(a, b) -> float:
    sa = {_normalize(x) for x in a if isinstance(x, str)}
    sb = {_normalize(x) for x in b if isinstance(x, str)}
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / len(sa | sb)


def _match(phase: str, a, b) -> float | None:
    """Per-phase agreement score in [0, 1]. None if shape is unusable."""
    if a is None or b is None:
        return None
    # stage phases
    if phase == "selection":
        return 1.0 if a.contains_proposition == b.contains_proposition else 0.0
    if phase == "disambiguation":
        if a.can_be_disambiguated != b.can_be_disambiguated:
            return 0.0
        if not a.can_be_disambiguated:
            return 1.0
        return (
            1.0
            if _normalize(a.decontextualized_sentence or "") == _normalize(b.decontextualized_sentence or "")
            else 0.0
        )
    if phase == "decomposition":
        return _jaccard([c.proposition for c in a.claims], [c.proposition for c in b.claims])
    # eval phases
    if phase == "invalid_sentence":
        return 1.0 if a.is_invalid == b.is_invalid else 0.0
    if phase == "element":
        return _jaccard(a.elements, b.elements)
    if phase == "entailment":
        return 1.0 if a.entailed == b.entailed else 0.0
    if phase == "decontextualization":
        return 1.0 if _normalize(a.c_max_text or "") == _normalize(b.c_max_text or "") else 0.0
    if phase == "invalid_claim":
        return 1.0 if a.is_invalid == b.is_invalid else 0.0
    return None


def _agreement(rows: list[dict]) -> None:
    if not rows:
        return
    by_phase_unit: dict[tuple[str, int], dict[str, dict]] = defaultdict(dict)
    for r in rows:
        by_phase_unit[(r["phase"], r["unit"])][r["path"]] = r

    print(f"{'phase':<22s} {'paired':>7s} {'agreement':>10s}")
    by_phase: dict[str, list[float]] = defaultdict(list)
    for (phase, _unit), by_path in by_phase_unit.items():
        prose = by_path.get("prose")
        struct = by_path.get("structured")
        if not prose or not struct or not prose["ok"] or not struct["ok"]:
            continue
        score = _match(phase, prose["result"], struct["result"])
        if score is not None:
            by_phase[phase].append(score)
    for phase in sorted(by_phase):
        scores = by_phase[phase]
        mean = sum(scores) / len(scores) if scores else float("nan")
        print(f"{phase:<22s} {len(scores):>7d} {mean:>10.3f}")


print("=== extraction agreement (prose vs structured) ===")
_agreement(stage_rows)
print("\n=== evaluation agreement (prose vs structured) ===")
_agreement(eval_rows)

# %%
# Persist raw rows so a follow-up notebook can slice without re-billing.
# Results aren't pydantic models; dump `result` via `.model_dump()` when present.
import json  # noqa: E402


def _dump(rows: list[dict], path: Path) -> None:
    if not rows:
        return
    with path.open("w") as fh:
        for r in rows:
            out = dict(r)
            if out.get("result") is not None and hasattr(out["result"], "model_dump"):
                out["result"] = out["result"].model_dump()
            fh.write(json.dumps(out, default=str) + "\n")
    print(f"wrote {len(rows)} rows -> {path}")


_dump(stage_rows, BAKEOFF_DIR / "stages.jsonl")
_dump(eval_rows, BAKEOFF_DIR / "evaluation.jsonl")

# %% [markdown]
# As of 17 Apr 2026 with `gpt-5.4-mini`, the bakeoff shows:
# - low agreement between prose and structured adapters on most dimensions.
# - prose is faster and cheaper
