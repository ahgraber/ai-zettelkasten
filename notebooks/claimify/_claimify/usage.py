"""Per-call usage capture + aggregation for pydantic-ai agents on OpenRouter.

## How cost is recovered

Use pydantic-ai's `OpenRouterModel` (pydantic_ai/models/openrouter.py)
instead of `OpenAIChatModel + OpenRouterProvider`.  `OpenRouterModel` maps
`usage.cost` into `ModelResponse.provider_details['cost']`.
`_find_cost` below reads it back.

## Fallback: out-of-band generation lookup (not wired in)

`_resolve_openrouter_cost` below is a fully-implemented async helper that
calls `GET https://openrouter.ai/api/v1/generation?id=<response_id>` with
retry/backoff.  It is **not** called by `extract_usage`; use it when:
- The subclass hook breaks (e.g. pydantic-ai renames `_process_provider_details`).
- You need post-hoc reconciliation outside the live call path.

To wire it back in, add to `extract_usage` after `_find_cost` returns None:
    if cost is None:
        response_id = _response_id(result)
        cost = await _resolve_openrouter_cost(response_id, api_key=key)
and make `extract_usage` async again.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
import logging
import os
from statistics import mean, median
from typing import Any

import httpx
from pydantic_ai.messages import ModelResponse

from _claimify.models import UsageSample

logger = logging.getLogger(__name__)

# ---------- primary path: read cost from provider_details ----------


def _coerce_int(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _find_cost(provider_details: dict[str, Any] | None) -> float | None:
    """Read the cost field that `OpenRouterModel` injects into `provider_details`.

    `OpenRouterModel` (pydantic_ai/models/openrouter.py) maps `usage.cost` to
    `provider_details['cost']`.  The extra keys are fallbacks for schema drift.
    """
    if not provider_details:
        return None
    for key in ("cost", "upstream_inference_cost", "total_cost"):
        if key in provider_details:
            try:
                return float(provider_details[key])
            except (TypeError, ValueError):
                continue
    return None


def extract_usage(result: Any, *, model: str) -> UsageSample:
    """Build a `UsageSample` from a pydantic-ai agent run result.

    Token counts come from `result.usage()`.  Cost is read from
    `ModelResponse.provider_details['cost']` — populated by `OpenRouterModel`
    when the response carries `usage.cost`.  Returns `cost_usd=None` when
    the model is not `OpenRouterModel` or cost was absent.
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


# ---------- fallback: out-of-band generation lookup (not wired in) ----------

OPENROUTER_GENERATION_URL = "https://openrouter.ai/api/v1/generation"

_COST_CACHE: dict[str, float | None] = {}
_HTTP_CLIENT: httpx.AsyncClient | None = None
_HTTP_LOCK = asyncio.Lock()


async def _get_http_client() -> httpx.AsyncClient:
    global _HTTP_CLIENT
    async with _HTTP_LOCK:
        if _HTTP_CLIENT is None:
            _HTTP_CLIENT = httpx.AsyncClient(timeout=10.0)
        return _HTTP_CLIENT


def _response_id(result: Any) -> str | None:
    messages = getattr(result, "all_messages", None)
    if not callable(messages):
        return None
    for msg in reversed(messages()):
        if isinstance(msg, ModelResponse) and getattr(msg, "provider_response_id", None):
            return msg.provider_response_id
    return None


async def _resolve_openrouter_cost(
    response_id: str | None,
    *,
    api_key: str | None = None,
    attempts: int = 4,
    initial_backoff_s: float = 0.5,
) -> float | None:
    """Fetch a generation's `total_cost` from OpenRouter (fallback path).

    Not called by `extract_usage`.  See module docstring for wiring instructions.

    Returns None when:
    - `response_id` or `api_key` is absent.
    - The generation is not yet indexed after `attempts` retries (404 with backoff).
    - Any other HTTP or parse error.
    """
    key = api_key or os.environ.get("OPENROUTER_API_KEY") or os.environ.get("_OPENROUTER_API_KEY")
    if not response_id or not key:
        return None
    if response_id in _COST_CACHE:
        return _COST_CACHE[response_id]

    client = await _get_http_client()
    headers = {"Authorization": f"Bearer {key}"}
    backoff = initial_backoff_s
    for _ in range(attempts):
        try:
            r = await client.get(OPENROUTER_GENERATION_URL, params={"id": response_id}, headers=headers)
        except httpx.HTTPError as exc:
            logger.debug("generation lookup transport error id=%s: %s", response_id, exc)
            await asyncio.sleep(backoff)
            backoff *= 2
            continue

        if r.status_code == 404:
            await asyncio.sleep(backoff)
            backoff *= 2
            continue
        if r.status_code >= 400:
            logger.debug("generation lookup %s for id=%s: %s", r.status_code, response_id, r.text[:200])
            break

        payload = r.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, dict):
            for key_name in ("total_cost", "cost", "cost_usd"):
                if key_name in data:
                    try:
                        value = float(data[key_name])
                        _COST_CACHE[response_id] = value
                        return value
                    except (TypeError, ValueError):
                        continue
        logger.debug("generation lookup id=%s returned no cost: %s", response_id, str(payload)[:200])
        break

    _COST_CACHE[response_id] = None
    return None


# ---------- aggregation ----------


def summarize(samples: Iterable[UsageSample]) -> dict[str, Any]:
    """Total / mean / median token + cost stats for a collection of samples."""
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
    """Group samples by a `UsageSample` attribute and summarize each bucket."""
    buckets: dict[str, list[UsageSample]] = {}
    for s in samples:
        k = str(getattr(s, key))
        buckets.setdefault(k, []).append(s)
    return {k: summarize(v) for k, v in buckets.items()}
