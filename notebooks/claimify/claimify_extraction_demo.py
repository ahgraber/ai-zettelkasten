#!/usr/bin/env python3
"""Claimify extraction driver.

Runs Contextualize -> Selection -> Disambiguation -> Decomposition across a
handful of KaraKeep bookmarks and writes one JSONL per doc under
`data/claimify_demo/extraction/`.

See:
- docs/superpowers/specs/2026-04-14-claimify-demo-design.md
- docs/superpowers/specs/2026-04-14-claimify-demo-plan.md (Milestone 7)
"""

# %%
from __future__ import annotations

import logging
import os
from pathlib import Path
import random

from _claimify.contextualize import contextualize_section, make_context_agent
from _claimify.io import (
    EXTRACTION_DIR,
    ensure_punkt_tab,
    load_docs,
    read_extraction_jsonl,
    write_extraction_jsonl,
)
from _claimify.models import ClaimRecord, FailedRecord, SkippedRecord
from _claimify.pipeline import (
    extract_claims,
    extraction_question,
    make_decomposition_runner,
    make_disambiguation_runner,
    make_selection_runner,
)
from _claimify.structuring import split_by_headings
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
# Five demo docs. Swap in/out; the commented entries below are alternates that
# exercise different source shapes (PDF-derived, HTML-derived, arXiv, etc.).
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
docs = load_docs(KARAKEEP_IDS)
for d in docs:
    print(f"{d.title[:60]:60s}  chars={len(d.markdown):>7}  source={d.source}")

# %%
# Model IDs: fill in real OpenRouter model slugs before running.
# See https://openrouter.ai/models for the registry.
CONTEXT_MODEL = "anthropic/claude-haiku-4-5"  # cache-friendly tier
SELECTION_MODEL = "openai/gpt-5-mini"
DISAMBIG_MODEL = "openai/gpt-5-mini"
DECOMP_MODEL = "openai/gpt-5-mini"

# Per-stage adapter path ("prose" or "structured"). The M5.5 experiment picks
# the winner; keep both wired so swapping costs nothing.
SELECTION_PATH = "prose"
DISAMBIG_PATH = "prose"
DECOMP_PATH = "prose"

context_agent = make_context_agent(CONTEXT_MODEL, api_key=OPENROUTER_API_KEY)
selection_runner = make_selection_runner(SELECTION_MODEL, path=SELECTION_PATH, api_key=OPENROUTER_API_KEY)
disambiguation_runner = make_disambiguation_runner(DISAMBIG_MODEL, path=DISAMBIG_PATH, api_key=OPENROUTER_API_KEY)
decomposition_runner = make_decomposition_runner(DECOMP_MODEL, path=DECOMP_PATH, api_key=OPENROUTER_API_KEY)

# %%
# Cost guard: contextualize one doc's sections and surface rough cost signals.
# Set RUN_CONTEXT_PROBE=False to skip.
RUN_CONTEXT_PROBE = True


async def _probe_context(doc) -> None:
    sections = split_by_headings(doc.markdown)
    print(f"probe doc={doc.title!r} sections={len(sections)}")
    for idx, section in enumerate(sections[:3]):
        ctx = await contextualize_section(context_agent, doc, section)
        print(f"  [{idx}] path={'/'.join(section.heading_path) or '<lead>'}: {ctx[:100]!r}")


if RUN_CONTEXT_PROBE:
    await _probe_context(docs[0])

# %%
# Rough cost estimate: sum sentence counts per doc. Each sentence fires up to
# three stage calls (selection/disambig/decomp), plus one contextualize call
# per section. Use this to decide whether to flip RUN_FULL.
ensure_punkt_tab()
from nltk.tokenize import sent_tokenize  # noqa: E402

total_sentences = 0
total_sections = 0
for d in docs:
    secs = split_by_headings(d.markdown)
    total_sections += len(secs)
    for s in secs:
        total_sentences += len(sent_tokenize(s.content))
print(f"docs={len(docs)}  sections={total_sections}  sentences={total_sentences}")
print(
    f"upper-bound LLM calls: contextualize={total_sections} "
    f"stages={total_sentences * 3}  (selection filters a chunk of these)"
)
print("# Flip RUN_FULL=True below to proceed")

# %%
RUN_FULL = False


async def _run_extraction(doc) -> Path:
    records = await extract_claims(
        doc,
        context_agent=context_agent,
        selection=selection_runner,
        disambiguation=disambiguation_runner,
        decomposition=decomposition_runner,
        p=5,
        f=5,
        question_for=extraction_question,
    )
    path = write_extraction_jsonl(doc.aizk_uuid, records)
    n_claim = sum(1 for r in records if isinstance(r, ClaimRecord))
    n_skip = sum(1 for r in records if isinstance(r, SkippedRecord))
    n_fail = sum(1 for r in records if isinstance(r, FailedRecord))
    print(f"{doc.title[:60]:60s}  claims={n_claim}  skipped={n_skip}  failed={n_fail}  -> {path.name}")
    return path


if RUN_FULL:
    for doc in docs:
        await _run_extraction(doc)


# %%
# Inspection: load all extraction JSONL back and summarize.
def _load_all() -> dict:
    by_doc: dict[str, list] = {}
    for d in docs:
        path = EXTRACTION_DIR / f"{d.aizk_uuid}.jsonl"
        if path.exists():
            by_doc[d.title] = read_extraction_jsonl(d.aizk_uuid)
    return by_doc


by_doc = _load_all()
for title, records in by_doc.items():
    claims = [r for r in records if isinstance(r, ClaimRecord)]
    skipped = [r for r in records if isinstance(r, SkippedRecord)]
    failed = [r for r in records if isinstance(r, FailedRecord)]
    print(f"{title[:60]:60s}  claims={len(claims)}  skipped={len(skipped)}  failed={len(failed)}")

# %%
# Random claim sample across all docs.
all_claims = [r for records in by_doc.values() for r in records if isinstance(r, ClaimRecord)]
if all_claims:
    for r in random.sample(all_claims, k=min(5, len(all_claims))):
        c = r.claim
        path = " > ".join(c.heading_path) or "<lead>"
        print(f"[{path}]  {c.claim.proposition}")
        if c.claim.essential_context:
            print(f"    ctx: {c.claim.essential_context}")
        print(f"    sentence: {c.sentence!r}")
        print()

# %%
# Failure / skipped detail for debugging.
for title, records in by_doc.items():
    for r in records:
        if isinstance(r, FailedRecord):
            f = r.failure
            print(f"FAIL [{title[:40]}] section={f.section_idx} sent={f.sentence_idx} stage={f.stage}: {f.error}")
        elif isinstance(r, SkippedRecord):
            a = r.artifact
            print(f"SKIP [{title[:40]}] kind={a.kind} pos={a.position}: {a.note}")

# %%
