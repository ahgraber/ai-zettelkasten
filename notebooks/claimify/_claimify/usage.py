"""Per-call usage capture + aggregation for pydantic-ai agents on OpenRouter.

Pydantic-ai's `result.usage()` returns a `RunUsage` with token counts but no
dollar amount. OpenRouter surfaces per-call cost in the response body when
`extra_body={"usage": {"include": True}}` is set; pydantic-ai passes that
through to `ModelResponse.provider_details`. We merge both here so every
LLM call produces a single `UsageSample`.
"""

from __future__ import annotations

from collections.abc import Iterable
from statistics import mean, median
from typing import Any

from pydantic_ai.messages import ModelResponse

from _claimify.models import UsageSample

# Keys OpenRouter uses to report cost within the usage block of its response.
_COST_KEYS = ("cost", "total_cost", "cost_usd")


def _coerce_int(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _find_cost(provider_details: dict[str, Any] | None) -> float | None:
    """Recursively search OpenRouter's provider_details for a cost field.

    OpenRouter has shifted the location of `cost` in its payload across
    versions; the value may sit at the top level, inside a nested `usage`
    block, or under `cost_details`. A shallow recursive scan is cheap and
    robust enough for a prototype.
    """
    if not provider_details:
        return None

    def _walk(node: Any, depth: int = 0) -> float | None:
        if depth > 4 or node is None:
            return None
        if isinstance(node, dict):
            for key in _COST_KEYS:
                if key in node:
                    try:
                        return float(node[key])
                    except (TypeError, ValueError):
                        pass
            for v in node.values():
                found = _walk(v, depth + 1)
                if found is not None:
                    return found
        return None

    return _walk(provider_details)


def extract_usage(result: Any, *, model: str) -> UsageSample:
    """Build a `UsageSample` from a pydantic-ai agent run result.

    Token counts come from `result.usage()`. Cost (when available) is read
    from the final `ModelResponse.provider_details`, which OpenRouter
    populates when usage-reporting is enabled.
    """
    usage = result.usage()
    input_tokens = _coerce_int(getattr(usage, "input_tokens", 0))
    output_tokens = _coerce_int(getattr(usage, "output_tokens", 0))
    total_tokens = _coerce_int(getattr(usage, "total_tokens", 0)) or (input_tokens + output_tokens)
    cache_read = _coerce_int(getattr(usage, "cache_read_tokens", 0))
    cache_write = _coerce_int(getattr(usage, "cache_write_tokens", 0))
    requests = _coerce_int(getattr(usage, "requests", 1)) or 1

    cost: float | None = None
    messages = getattr(result, "all_messages", None)
    if callable(messages):
        for msg in reversed(messages()):
            if isinstance(msg, ModelResponse):
                cost = _find_cost(msg.provider_details)
                if cost is not None:
                    break

    return UsageSample(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        cost_usd=cost,
        requests=requests,
    )


def summarize(samples: Iterable[UsageSample]) -> dict[str, Any]:
    """Total / mean / median token + cost stats for a bunch of samples."""
    samples = list(samples)
    if not samples:
        return {
            "calls": 0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_tokens": 0,
            "total_cache_read_tokens": 0,
            "total_cache_write_tokens": 0,
            "total_cost_usd": None,
            "mean_total_tokens": 0.0,
            "median_total_tokens": 0.0,
            "mean_cost_usd": None,
            "median_cost_usd": None,
        }

    totals = [s.total_tokens for s in samples]
    costs = [s.cost_usd for s in samples if s.cost_usd is not None]
    return {
        "calls": len(samples),
        "total_input_tokens": sum(s.input_tokens for s in samples),
        "total_output_tokens": sum(s.output_tokens for s in samples),
        "total_tokens": sum(totals),
        "total_cache_read_tokens": sum(s.cache_read_tokens for s in samples),
        "total_cache_write_tokens": sum(s.cache_write_tokens for s in samples),
        "total_cost_usd": sum(costs) if costs else None,
        "mean_total_tokens": mean(totals) if totals else 0.0,
        "median_total_tokens": median(totals) if totals else 0.0,
        "mean_cost_usd": mean(costs) if costs else None,
        "median_cost_usd": median(costs) if costs else None,
    }


def summarize_by(samples: Iterable[UsageSample], key: str) -> dict[str, dict[str, Any]]:
    """Group samples by an attribute (e.g., `phase` from parent record) and summarize each bucket.

    Since `UsageSample` itself doesn't carry a phase, callers typically pre-group
    by inspecting the enclosing `UsageRecord`/`EvalRecord`. This helper works for
    any attribute present on the sample (e.g., `model`).
    """
    buckets: dict[str, list[UsageSample]] = {}
    for s in samples:
        k = str(getattr(s, key))
        buckets.setdefault(k, []).append(s)
    return {k: summarize(v) for k, v in buckets.items()}
