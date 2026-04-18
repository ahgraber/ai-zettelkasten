"""OpenRouter client factory with built-in retry transport.

## Usage

    from _claimify.openrouter import make_openrouter_provider
    from pydantic_ai.models.openrouter import OpenRouterModel

    provider = make_openrouter_provider(api_key=OPENROUTER_API_KEY)
    model = OpenRouterModel("openai/gpt-5-mini", provider=provider)

`make_openrouter_provider` wires `AsyncTenacityTransport` (from
`pydantic_ai.retries`) into the provider's httpx client so transient failures
retry automatically: 429s honour OpenRouter's `Retry-After` header, 5xx and
network-layer errors back off exponentially.

## Why OpenRouterModel, not OpenAIChatModel + OpenRouterProvider?

`OpenAIChatModel._map_usage` filters `response.usage` by `isinstance(v, int)`,
silently dropping OpenRouter's `usage.cost` float.  `OpenRouterModel` (in
`pydantic_ai/models/openrouter.py`) bypasses this via `_OpenRouterUsage` and
maps cost to `ModelResponse.provider_details['cost']` before the filter runs.
See `_claimify/usage.py:_find_cost` for the read side.

## Why `http_client=` and not `openai_client=`?

Passing a prebuilt `AsyncOpenAI` via `openai_client=` bypasses the provider's
`api_key` validation (see `OpenRouterProvider.__init__`: the missing-key check
only runs when `openai_client is None`).  Passing `http_client=` lets the
provider build the `AsyncOpenAI` itself so missing keys fail fast at
construction instead of at first request.

## Fallback subclass (not in use)

If `OpenRouterModel`'s cost mapping breaks in a future pydantic-ai version,
override `_process_provider_details` on `OpenAIChatModel` instead:

    class _OpenRouterCostModel(OpenAIChatModel):
        def _process_provider_details(self, response):
            details = dict(super()._process_provider_details(response) or {})
            if response.usage is not None:
                raw = response.usage.model_dump(exclude_none=True)
                for key in ("cost", "total_cost", "cost_usd"):
                    if key in raw:
                        try:
                            details["cost"] = float(raw[key])
                            break
                        except (TypeError, ValueError):
                            continue
            return details or None

See also `_claimify/usage.py:_resolve_openrouter_cost` for an out-of-band HTTP
fallback via `GET /api/v1/generation?id=<response_id>`.
"""

from __future__ import annotations

import httpx
from httpx import HTTPStatusError
from pydantic_ai.providers.openrouter import OpenRouterProvider
from pydantic_ai.retries import AsyncTenacityTransport, RetryConfig, wait_retry_after

from tenacity import retry_if_exception_type, stop_after_attempt

# Retry on transient transport failures (timeouts, connection resets, DNS, etc.)
# plus HTTP-level signals we explicitly raise from `_validate_response` below.
_RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    HTTPStatusError,
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
)


def _validate_response(response: httpx.Response) -> None:
    """Convert retryable HTTP statuses to exceptions so tenacity can retry them.

    - 429: rate limited; `wait_retry_after` will honour `Retry-After`.
    - 5xx: transient server errors.

    4xx other than 429 surface immediately as non-retryable errors from the
    caller's `.raise_for_status()` or normal response handling.
    """
    if response.status_code == 429 or response.status_code >= 500:
        response.raise_for_status()


def make_openrouter_provider(
    api_key: str | None = None,
    *,
    max_retries: int = 5,
    max_retry_wait_s: float = 120.0,
) -> OpenRouterProvider:
    """Return an `OpenRouterProvider` whose HTTP client retries transient failures.

    Retries cover:

    - HTTP 429 with `Retry-After`-aware backoff via `wait_retry_after`.
    - HTTP 5xx from OpenRouter or upstream providers.
    - httpx transport errors: timeouts, connection resets, DNS failures,
      `RemoteProtocolError`.

    Non-retryable: 4xx other than 429 (auth, bad request, etc.), any error
    outside `_RETRYABLE_EXCEPTIONS`.

    Args:
        api_key: OpenRouter API key.  Falls back to `OPENROUTER_API_KEY`
            inside `OpenRouterProvider`.  A missing key raises `UserError`
            at construction time because we use the `http_client=` seam,
            which preserves the provider's api-key validation.
        max_retries: Maximum retry attempts per request (default 5).
        max_retry_wait_s: Hard ceiling on wait time between retries in seconds
            (default 120).  OpenRouter sometimes returns Retry-After values
            above 60s during sustained bursts.
    """
    transport = AsyncTenacityTransport(
        RetryConfig(
            retry=retry_if_exception_type(_RETRYABLE_EXCEPTIONS),
            wait=wait_retry_after(max_wait=max_retry_wait_s),
            stop=stop_after_attempt(max_retries),
            reraise=True,
        ),
        validate_response=_validate_response,
    )
    http_client = httpx.AsyncClient(transport=transport)
    return OpenRouterProvider(api_key=api_key, http_client=http_client)
