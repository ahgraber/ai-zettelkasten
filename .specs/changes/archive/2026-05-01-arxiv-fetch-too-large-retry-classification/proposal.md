# Proposal: arxiv-fetch-too-large-retry-classification

## Intent

`fetch_arxiv_pdf()` in `src/aizk/conversion/utilities/fetch_helpers.py` preserves only `EgressPolicyError` from the underlying `egress_fetch_bytes` call.
Any other exception — including `FetchTooLargeError`, which is the typed signal that a response exceeded the configured byte cap — is caught by the generic `except Exception` and re-raised as `ArxivPdfFetchError`.

`FetchTooLargeError.retryable` is `False`; `ArxivPdfFetchError.retryable` is `True` (inherited from `FetchError`).
The wrap therefore flips the retry classification from "fail permanently" to "retry forever," which defeats the size cap as a stop condition: the worker will keep re-fetching the same oversized arXiv PDF on every retry instead of giving up.

The other fetchers (`UrlFetcher`, the egress helpers in `html_prefetch.py`) already preserve `FetchTooLargeError` unwrapped.
This change closes the inconsistency by making the arXiv path do the same.

## Scope

**In scope:**

- Preserve `FetchTooLargeError` unwrapped from `fetch_arxiv_pdf()` so
  worker retry classification sees the non-retryable signal
- Generalize the rule so any future `FetchError` subclass with
  `retryable = False` (or `EgressPolicyError`) propagates without being
  re-wrapped as `ArxivPdfFetchError`
- Unit-test coverage for the propagation rule on the arXiv path

**Out of scope:**

- Changing the deny-list, redirect, or DNS-pinning behavior of
  `egress_fetch_bytes`
- Changing `FetchError`'s default `retryable = True`
- Touching unrelated callers of `fetch_arxiv_pdf` (e.g., the worker's
  `fetch_arxiv` orchestration) beyond letting the typed error pass
  through
- README or env-var documentation cleanup (tracked separately)

## Approach

`fetch_arxiv_pdf()` already special-cases `EgressPolicyError` to keep its non-retryable classification visible to the job runner.
The fix is to widen that special case to any non-retryable typed error — concretely `FetchTooLargeError`, and structurally any error whose `retryable` ClassVar is `False`.
The simplest, most local expression of this is to re-raise `EgressPolicyError` and `FetchTooLargeError` unwrapped, since `FetchTooLargeError` is the only `FetchError` subclass currently raised by `egress_fetch_bytes` that is non-retryable.
Other transient `FetchError` subclasses raised by the underlying HTTP path stay wrapped as `ArxivPdfFetchError` (retryable) per the existing contract.

## Schema Impact

OpenAPI is unchanged.
Database shape is unchanged.
This change is an internal control-flow correction; no client-visible surface moves.
