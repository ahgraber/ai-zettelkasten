"""Claimify evaluation agents: six per-dimension factories + bundle.

Each factory wires one of `aizk.ai.claimify.prompts.evaluation.*` to an
OpenRouter chat model via pydantic-ai. The returned async runner takes the
inputs that the prompt's USER_TEMPLATE actually uses and returns a typed
pydantic result. Adapter path ("prose" | "structured") is chosen per-factory
per the M5.5 experiment.

Aggregation of per-call verdicts into section- or claim-level result models
(`InvalidSentencesResult`, `InvalidClaimsResult`) lives in M9's orchestrator.
"""

from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel
from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.models.openai import OpenAIChatModelSettings
from pydantic_ai.models.openrouter import OpenRouterModel

from _claimify.adapters import (
    parse_coverage,
    parse_decontextualization,
    parse_element,
    parse_entailment,
    parse_invalid_claim,
    parse_invalid_sentence,
    render_template,
    with_schema_suffix,
)
from _claimify.io import write_evaluation_jsonl
from _claimify.models import (
    ClaimRecord,
    CoverageResult,
    DecontextResult,
    ElementResult,
    EntailmentResult,
    EvalRecord,
    ExtractionRecord,
    InvalidClaimVerdict,
    InvalidSentenceVerdict,
    LoadedDoc,
    UsageSample,
)
from _claimify.pipeline import build_sentence_contexts, default_question
from _claimify.structuring import split_by_headings
from _claimify.usage import extract_usage
from aizk.ai.claimify.prompts.evaluation import (
    coverage as coverage_prompts,
    decontextualization as decontext_prompts,
    element as element_prompts,
    entailment as entailment_prompts,
    invalid_claims as invalid_claims_prompts,
    invalid_sentences as invalid_sentences_prompts,
)

logger = logging.getLogger(__name__)

ALL_DIMENSIONS: tuple[str, ...] = (
    "invalid_sentence",
    "element",
    "coverage",
    "entailment",
    "decontextualization",
    "invalid_claim",
)

AdapterPath = Literal["prose", "structured"]

InvalidSentenceRunner = Callable[[str, str, str], Awaitable[tuple[InvalidSentenceVerdict, UsageSample]]]
ElementRunner = Callable[[str, str, str], Awaitable[tuple[ElementResult, UsageSample]]]
CoverageRunner = Callable[[str, str, dict[int, str], dict[int, str]], Awaitable[tuple[CoverageResult, UsageSample]]]
EntailmentRunner = Callable[[str, str, str, str], Awaitable[tuple[EntailmentResult, UsageSample]]]
DecontextRunner = Callable[[str, str, str, list[str], str], Awaitable[tuple[DecontextResult, UsageSample]]]
InvalidClaimRunner = Callable[[str], Awaitable[tuple[InvalidClaimVerdict, UsageSample]]]


def _is_anthropic(model: str) -> bool:
    return model.startswith("anthropic/") or model.startswith("claude")


def _settings_for(model: str) -> OpenAIChatModelSettings:
    """Compose model settings with OpenRouter usage reporting + Anthropic cache_control."""
    body: dict = {"usage": {"include": True}}
    if _is_anthropic(model):
        body["cache_control"] = {"type": "ephemeral"}
    return OpenAIChatModelSettings(extra_body=body)


def _build_agent(
    model: str,
    *,
    api_key: str | None,
    system_prompt: str,
    result_model: type[BaseModel],
    path: AdapterPath,
    deps_type: type | None = None,
) -> Agent:
    """Build a per-dimension agent.

    `output_retries=2` covers both structured-output schema failures and,
    when an `output_validator` is attached to a prose agent, parse failures
    from the per-dimension `parse_*` adapter.  Prose agents pass `deps_type`
    when the validator needs per-call context (e.g., `n_elements`, `claim`).
    """
    chat = OpenRouterModel(model, provider=make_openrouter_provider(api_key))
    settings = _settings_for(model)
    if path == "prose":
        return Agent(
            chat,
            output_type=str,
            system_prompt=system_prompt,
            model_settings=settings,
            deps_type=deps_type,
            output_retries=2,
        )
    return Agent(
        chat,
        output_type=result_model,
        system_prompt=with_schema_suffix(system_prompt, result_model),
        model_settings=settings,
        output_retries=2,
    )


def _format_indexed_dict(items: dict[int, str]) -> str:
    """Render a dict as the prompt's example format: `{\\n1: "...",\\n...}`."""
    body = ",\n".join(f"{k}: {v!r}" for k, v in items.items())
    return "{\n" + body + ",\n}"


def _format_list(items: list[str]) -> str:
    body = ",\n".join(repr(i) for i in items)
    return "[\n" + body + ",\n]"


# ---------- invalid_sentences ----------


def make_invalid_sentence_agent(
    model: str, *, path: AdapterPath = "prose", api_key: str | None = None
) -> InvalidSentenceRunner:
    agent = _build_agent(
        model,
        api_key=api_key,
        system_prompt=invalid_sentences_prompts.SYSTEM_PROMPT,
        result_model=InvalidSentenceVerdict,
        path=path,
    )

    async def run(question: str, excerpt: str, sentence: str) -> tuple[InvalidSentenceVerdict, UsageSample]:
        user = render_template(
            invalid_sentences_prompts.USER_TEMPLATE,
            question=question,
            excerpt=excerpt,
            sentence=sentence,
        )
        result = await agent.run(user)
        sample = extract_usage(result, model=model)
        if path == "prose":
            return parse_invalid_sentence(result.output), sample
        return result.output, sample

    return run


# ---------- element ----------


def make_element_agent(model: str, *, path: AdapterPath = "prose", api_key: str | None = None) -> ElementRunner:
    agent = _build_agent(
        model,
        api_key=api_key,
        system_prompt=element_prompts.SYSTEM_PROMPT,
        result_model=ElementResult,
        path=path,
    )

    async def run(question: str, excerpt: str, sentence: str) -> tuple[ElementResult, UsageSample]:
        user = render_template(
            element_prompts.USER_TEMPLATE,
            question=question,
            excerpt=excerpt,
            sentence=sentence,
        )
        result = await agent.run(user)
        sample = extract_usage(result, model=model)
        if path == "prose":
            return parse_element(result.output), sample
        return result.output, sample

    return run


# ---------- coverage ----------


def make_coverage_agent(model: str, *, path: AdapterPath = "prose", api_key: str | None = None) -> CoverageRunner:
    agent = _build_agent(
        model,
        api_key=api_key,
        system_prompt=coverage_prompts.SYSTEM_PROMPT,
        result_model=CoverageResult,
        path=path,
    )

    async def run(
        question: str,
        excerpt: str,
        claims: dict[int, str],
        elements: dict[int, str],
    ) -> tuple[CoverageResult, UsageSample]:
        user = render_template(
            coverage_prompts.USER_TEMPLATE,
            question=question,
            excerpt=excerpt,
            claims=_format_indexed_dict(claims),
            elements=_format_indexed_dict(elements),
        )
        result = await agent.run(user)
        sample = extract_usage(result, model=model)
        if path == "prose":
            return parse_coverage(result.output, n_elements=len(elements)), sample
        return result.output, sample

    return run


# ---------- entailment ----------


def make_entailment_agent(model: str, *, path: AdapterPath = "prose", api_key: str | None = None) -> EntailmentRunner:
    agent = _build_agent(
        model,
        api_key=api_key,
        system_prompt=entailment_prompts.SYSTEM_PROMPT,
        result_model=EntailmentResult,
        path=path,
    )

    async def run(question: str, excerpt: str, sentence: str, claim: str) -> tuple[EntailmentResult, UsageSample]:
        user = render_template(
            entailment_prompts.USER_TEMPLATE,
            question=question,
            excerpt=excerpt,
            sentence=sentence,
            claim=claim,
        )
        result = await agent.run(user)
        sample = extract_usage(result, model=model)
        if path == "prose":
            return parse_entailment(result.output), sample
        return result.output, sample

    return run


# ---------- decontextualization ----------


def make_decontextualization_agent(
    model: str, *, path: AdapterPath = "prose", api_key: str | None = None
) -> DecontextRunner:
    agent = _build_agent(
        model,
        api_key=api_key,
        system_prompt=decontext_prompts.SYSTEM_PROMPT,
        result_model=DecontextResult,
        path=path,
    )

    async def run(
        question: str,
        excerpt: str,
        sentence: str,
        all_claims: list[str],
        claim: str,
    ) -> tuple[DecontextResult, UsageSample]:
        user = render_template(
            decontext_prompts.USER_TEMPLATE,
            question=question,
            excerpt=excerpt,
            sentence=sentence,
            claims=_format_list(all_claims),
            claim=claim,
        )
        result = await agent.run(user)
        sample = extract_usage(result, model=model)
        if path == "prose":
            return parse_decontextualization(result.output, claim=claim), sample
        return result.output, sample

    return run


# ---------- invalid_claims ----------


def make_invalid_claim_agent(
    model: str, *, path: AdapterPath = "prose", api_key: str | None = None
) -> InvalidClaimRunner:
    agent = _build_agent(
        model,
        api_key=api_key,
        system_prompt=invalid_claims_prompts.SYSTEM_PROMPT,
        result_model=InvalidClaimVerdict,
        path=path,
    )

    async def run(claim: str) -> tuple[InvalidClaimVerdict, UsageSample]:
        user = render_template(invalid_claims_prompts.USER_TEMPLATE, claim=claim)
        result = await agent.run(user)
        sample = extract_usage(result, model=model)
        if path == "prose":
            return parse_invalid_claim(result.output), sample
        return result.output, sample

    return run


# ---------- bundle ----------


@dataclass(frozen=True)
class EvalAgentBundle:
    """A set of evaluation runners for one model, used by the M9 orchestrator."""

    model: str
    invalid_sentence: InvalidSentenceRunner
    element: ElementRunner
    coverage: CoverageRunner
    entailment: EntailmentRunner
    decontextualization: DecontextRunner
    invalid_claim: InvalidClaimRunner


def bundle_for(
    model: str,
    *,
    paths: dict[str, AdapterPath] | None = None,
    api_key: str | None = None,
) -> EvalAgentBundle:
    """Build an `EvalAgentBundle` for `model` with optional per-dimension paths.

    `paths` keys: "invalid_sentence", "element", "coverage", "entailment",
    "decontextualization", "invalid_claim". Missing keys default to "prose".
    """
    paths = paths or {}

    def p(key: str) -> AdapterPath:
        return paths.get(key, "prose")

    return EvalAgentBundle(
        model=model,
        invalid_sentence=make_invalid_sentence_agent(model, path=p("invalid_sentence"), api_key=api_key),
        element=make_element_agent(model, path=p("element"), api_key=api_key),
        coverage=make_coverage_agent(model, path=p("coverage"), api_key=api_key),
        entailment=make_entailment_agent(model, path=p("entailment"), api_key=api_key),
        decontextualization=make_decontextualization_agent(model, path=p("decontextualization"), api_key=api_key),
        invalid_claim=make_invalid_claim_agent(model, path=p("invalid_claim"), api_key=api_key),
    )


# ---------- orchestrator ----------


def _group_claims_by_sentence(
    records: Iterable[ExtractionRecord],
) -> dict[tuple[int, int], list[ClaimRecord]]:
    out: dict[tuple[int, int], list[ClaimRecord]] = defaultdict(list)
    for r in records:
        if isinstance(r, ClaimRecord):
            c = r.claim
            out[(c.section_idx, c.sentence_idx)].append(r)
    return out


async def _evaluate_sentence(
    bundle: EvalAgentBundle,
    doc: LoadedDoc,
    question: str,
    excerpt: str,
    section_idx: int,
    sentence_idx: int,
    sentence: str,
    claim_records: list[ClaimRecord],
    dimensions: set[str],
    sem: asyncio.Semaphore,
) -> list[EvalRecord]:
    records: list[EvalRecord] = []
    all_claim_texts = [cr.claim.claim.proposition for cr in claim_records]

    def emit(dimension: str, claim_idx: int | None, result: BaseModel, usage: UsageSample) -> None:
        records.append(
            EvalRecord(
                doc_uuid=doc.aizk_uuid,
                section_idx=section_idx,
                sentence_idx=sentence_idx,
                claim_idx=claim_idx,
                dimension=dimension,
                model=bundle.model,
                result_json=result.model_dump(),
                raw=None,
                usage=usage,
            )
        )

    async def _call(coro):
        async with sem:
            return await coro

    if "invalid_sentence" in dimensions:
        try:
            v, u = await _call(bundle.invalid_sentence(question, excerpt, sentence))
            emit("invalid_sentence", None, v, u)
        except Exception as exc:
            logger.warning("invalid_sentence failed model=%s: %s", bundle.model, exc)

    elements_result: ElementResult | None = None
    # element/coverage compare extracted claims against sentence elements;
    # both are meaningless for sentences with zero claims, so skip them there
    # to avoid doubling the bill.
    if ("element" in dimensions or "coverage" in dimensions) and claim_records:
        try:
            elements_result, u = await _call(bundle.element(question, excerpt, sentence))
            if "element" in dimensions:
                emit("element", None, elements_result, u)
        except Exception as exc:
            logger.warning("element failed model=%s: %s", bundle.model, exc)

    if "coverage" in dimensions and elements_result is not None and all_claim_texts:
        claims_dict = {i + 1: t for i, t in enumerate(all_claim_texts)}
        elements_dict = {i + 1: e for i, e in enumerate(elements_result.elements)}
        try:
            cov, u = await _call(bundle.coverage(question, excerpt, claims_dict, elements_dict))
            emit("coverage", None, cov, u)
        except Exception as exc:
            logger.warning("coverage failed model=%s: %s", bundle.model, exc)

    for claim_idx, _ in enumerate(claim_records):
        claim_text = all_claim_texts[claim_idx]
        if "entailment" in dimensions:
            try:
                ent, u = await _call(bundle.entailment(question, excerpt, sentence, claim_text))
                emit("entailment", claim_idx, ent, u)
            except Exception as exc:
                logger.warning("entailment failed model=%s claim=%d: %s", bundle.model, claim_idx, exc)
        if "decontextualization" in dimensions:
            try:
                dec, u = await _call(
                    bundle.decontextualization(question, excerpt, sentence, all_claim_texts, claim_text)
                )
                emit("decontextualization", claim_idx, dec, u)
            except Exception as exc:
                logger.warning("decontext failed model=%s claim=%d: %s", bundle.model, claim_idx, exc)
        if "invalid_claim" in dimensions:
            try:
                ic, u = await _call(bundle.invalid_claim(claim_text))
                emit("invalid_claim", claim_idx, ic, u)
            except Exception as exc:
                logger.warning("invalid_claim failed model=%s claim=%d: %s", bundle.model, claim_idx, exc)

    return records


async def evaluate_claims(
    doc: LoadedDoc,
    records: Iterable[ExtractionRecord],
    *,
    tiers: dict[str, list[str]],
    dimensions: Iterable[str] = ALL_DIMENSIONS,
    paths: dict[str, AdapterPath] | None = None,
    api_key: str | None = None,
    max_parallel: int = 8,
    question_for: Callable[[LoadedDoc, object], str] = default_question,
) -> Path:
    """Run each dimension across every (tier × model) and persist `EvalRecord`s.

    Iterates over every tokenized sentence in the doc so dimensions like
    `invalid_sentence` can score sentences Selection dropped (zero-claim
    sentences). Per-claim dimensions fire only where `ClaimRecord`s exist.

    Tiers run sequentially so cost-guard prints are accurate; within a tier,
    sentence/claim calls fan out under `Semaphore(max_parallel)`.
    """
    dim_set = set(dimensions)
    records = list(records)
    claims_by_sent = _group_claims_by_sentence(records)
    sections = split_by_headings(doc.markdown)

    all_eval: list[EvalRecord] = []
    for tier_name, model_ids in tiers.items():
        logger.info("tier=%s models=%s", tier_name, model_ids)
        sem = asyncio.Semaphore(max_parallel)
        bundles = [bundle_for(m, paths=paths, api_key=api_key) for m in model_ids]

        tier_tasks: list[Awaitable[list[EvalRecord]]] = []
        for section_idx, section in enumerate(sections):
            question = question_for(doc, section)
            excerpt = section.content
            contexts = build_sentence_contexts(section, section_idx, p=0, f=0)
            for ctx in contexts:
                sent_claims = claims_by_sent.get((section_idx, ctx.sentence_idx), [])
                for bundle in bundles:
                    tier_tasks.append(
                        _evaluate_sentence(
                            bundle,
                            doc,
                            question,
                            excerpt,
                            section_idx,
                            ctx.sentence_idx,
                            ctx.sentence,
                            sent_claims,
                            dim_set,
                            sem,
                        )
                    )

        batched = await asyncio.gather(*tier_tasks)
        for group in batched:
            all_eval.extend(group)

    return write_evaluation_jsonl(doc.aizk_uuid, all_eval)


# ---------- aggregation (pure) ----------


def _normalize_text(s: str) -> str:
    return s.strip().lower().rstrip(".!?;:")


def baseline_majority(verdicts_by_model: dict[str, object]) -> object | None:
    """Majority vote across models for one (unit, dimension).

    Handles bools, lists of bools (elementwise), and strings (normalized-text
    majority). Returns None if no verdicts were supplied.
    """
    if not verdicts_by_model:
        return None
    values = list(verdicts_by_model.values())
    first = values[0]

    if isinstance(first, bool):
        counts = Counter(v for v in values if isinstance(v, bool))
        return counts.most_common(1)[0][0]

    if isinstance(first, list) and all(isinstance(x, bool) for x in first):
        if not all(isinstance(v, list) and len(v) == len(first) for v in values):
            return None
        return [Counter(v[i] for v in values).most_common(1)[0][0] for i in range(len(first))]

    if isinstance(first, list) and all(isinstance(x, str) for x in first):
        # element dimension: majority picks the list whose normalized-set
        # representation appears most often among models.
        def _norm_set(xs: list[str]) -> frozenset[str]:
            return frozenset(_normalize_text(x) for x in xs if isinstance(x, str))

        list_values = [v for v in values if isinstance(v, list) and all(isinstance(x, str) for x in v)]
        if not list_values:
            return None
        counts = Counter(_norm_set(v) for v in list_values)
        top_set, _ = counts.most_common(1)[0]
        for v in list_values:
            if _norm_set(v) == top_set:
                return v
        return None

    if isinstance(first, str):
        counts = Counter(_normalize_text(v) for v in values if isinstance(v, str))
        top_norm, _ = counts.most_common(1)[0]
        for v in values:
            if isinstance(v, str) and _normalize_text(v) == top_norm:
                return v
        return None

    return None


def cohens_kappa(a: list[object], b: list[object]) -> float:
    """Cohen's kappa for two equal-length label sequences.

    Uses `sklearn.metrics.cohen_kappa_score` when available; otherwise falls
    back to the 2-rater formula. Returns 0.0 on empty input.
    """
    if len(a) != len(b):
        raise ValueError(f"length mismatch: {len(a)} vs {len(b)}")
    if not a:
        return 0.0
    # κ is undefined when only one label appears across both raters. sklearn
    # returns NaN in that case; collapse to 1.0 on perfect match, 0.0 otherwise.
    if len({*a, *b}) < 2:
        return 1.0 if a == b else 0.0
    try:
        from sklearn.metrics import cohen_kappa_score

        return float(cohen_kappa_score(a, b))
    except ImportError:
        pass

    n = len(a)
    labels = sorted({*a, *b}, key=repr)
    if len(labels) < 2:
        return 1.0 if a == b else 0.0
    agree = sum(1 for x, y in zip(a, b, strict=False) if x == y)
    po = agree / n
    counts_a = Counter(a)
    counts_b = Counter(b)
    pe = sum((counts_a[l] * counts_b[l]) / (n * n) for l in labels)
    if pe == 1.0:
        return 1.0
    return (po - pe) / (1 - pe)


BOOL_DIMENSIONS = {"invalid_sentence", "entailment", "invalid_claim"}
LIST_BOOL_DIMENSIONS = {"coverage"}
ELEMENT_DIMENSIONS = {"element"}
TEXT_DIMENSIONS = {"decontextualization"}


def _verdict_key(rec: EvalRecord) -> tuple:
    return (rec.section_idx, rec.sentence_idx, rec.claim_idx, rec.dimension)


def _extract_verdict(rec: EvalRecord) -> object | None:
    d = rec.result_json
    if rec.dimension == "invalid_sentence":
        return d.get("is_invalid")
    if rec.dimension == "invalid_claim":
        return d.get("is_invalid")
    if rec.dimension == "entailment":
        return d.get("entailed")
    if rec.dimension == "coverage":
        return d.get("per_element_covered")
    if rec.dimension == "element":
        return d.get("elements")
    if rec.dimension == "decontextualization":
        return d.get("c_max_text")
    return None


def _jaccard(a: list[str], b: list[str]) -> float:
    sa = {_normalize_text(s) for s in a if isinstance(s, str)}
    sb = {_normalize_text(s) for s in b if isinstance(s, str)}
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / len(sa | sb)


def agreement_table(
    verdicts: Iterable[EvalRecord],
    tiers: dict[str, list[str]],
    *,
    baseline_tier: str = "baseline",
):
    """Per-model agreement vs. the baseline-tier majority; returns a DataFrame.

    The "baseline" is the majority vote among models in `tiers[baseline_tier]`
    (design §2.3). Metrics vary by dimension:

    - `invalid_sentence`, `entailment`, `invalid_claim`: Cohen's κ across units.
    - `coverage` (list[bool]): Cohen's κ on flattened per-element labels.
    - `element` (list[str]): mean Jaccard similarity across units.
    - `decontextualization`: two columns — `_exact` and `_normalized` match rate.
    """
    import pandas as pd

    verdicts = list(verdicts)
    baseline_models = set(tiers.get(baseline_tier, []))
    if not baseline_models:
        raise ValueError(f"baseline_tier {baseline_tier!r} not present in tiers: {list(tiers)}")
    all_models = sorted({m for models in tiers.values() for m in models})

    by_unit: dict[tuple, dict[str, object]] = defaultdict(dict)
    for rec in verdicts:
        value = _extract_verdict(rec)
        if value is None:
            continue
        by_unit[_verdict_key(rec)][rec.model] = value

    baselines: dict[tuple, object] = {}
    for key, verdicts_by_model in by_unit.items():
        baseline_votes = {m: v for m, v in verdicts_by_model.items() if m in baseline_models}
        bm = baseline_majority(baseline_votes)
        if bm is not None:
            baselines[key] = bm

    def _pair_lists(model: str, dim: str) -> tuple[list, list]:
        mvals: list = []
        bvals: list = []
        for key, verdicts_by_model in by_unit.items():
            if key[3] != dim or model not in verdicts_by_model or key not in baselines:
                continue
            mvals.append(verdicts_by_model[model])
            bvals.append(baselines[key])
        return mvals, bvals

    dims = sorted({rec.dimension for rec in verdicts})
    rows: list[dict[str, object]] = []
    for model in all_models:
        row: dict[str, object] = {"model": model}
        for dim in dims:
            mvals, bvals = _pair_lists(model, dim)
            n = len(mvals)
            if dim in BOOL_DIMENSIONS:
                row[dim] = cohens_kappa(mvals, bvals) if n else float("nan")
            elif dim in LIST_BOOL_DIMENSIONS:
                flat_m: list = []
                flat_b: list = []
                for mv, bv in zip(mvals, bvals, strict=False):
                    if isinstance(mv, list) and isinstance(bv, list) and len(mv) == len(bv):
                        flat_m.extend(mv)
                        flat_b.extend(bv)
                row[dim] = cohens_kappa(flat_m, flat_b) if flat_m else float("nan")
            elif dim in ELEMENT_DIMENSIONS:
                if n == 0:
                    row[dim] = float("nan")
                else:
                    scores = [
                        _jaccard(mv, bv)
                        for mv, bv in zip(mvals, bvals, strict=False)
                        if isinstance(mv, list) and isinstance(bv, list)
                    ]
                    row[dim] = (sum(scores) / len(scores)) if scores else float("nan")
            elif dim in TEXT_DIMENSIONS:
                if n == 0:
                    row[f"{dim}_exact"] = float("nan")
                    row[f"{dim}_normalized"] = float("nan")
                else:
                    pairs = [
                        (mv, bv)
                        for mv, bv in zip(mvals, bvals, strict=False)
                        if isinstance(mv, str) and isinstance(bv, str)
                    ]
                    if not pairs:
                        row[f"{dim}_exact"] = float("nan")
                        row[f"{dim}_normalized"] = float("nan")
                    else:
                        exact = sum(1 for mv, bv in pairs if mv == bv) / len(pairs)
                        normalized = sum(1 for mv, bv in pairs if _normalize_text(mv) == _normalize_text(bv)) / len(
                            pairs
                        )
                        row[f"{dim}_exact"] = exact
                        row[f"{dim}_normalized"] = normalized
            else:
                row[dim] = float("nan")
        rows.append(row)

    return pd.DataFrame(rows).set_index("model")
