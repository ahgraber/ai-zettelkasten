# Delta for pluggable-pipeline — arxiv-fetch-too-large-retry-classification

## ADDED Requirements

### Requirement: Preserve non-retryable fetch-error classification through fetcher dispatch

A fetcher that delegates to lower-level helpers (the egress helper, the
KaraKeep client, the arXiv export API) SHALL propagate any typed error
whose declared retry classification is non-retryable without re-wrapping
it as a retryable error.

This applies to at least:

- `EgressPolicyError` and its subclasses (already covered by the
  egress-policy requirement; restated here so the broader rule is
  explicit)
- `FetchTooLargeError`, raised by the egress helper when a response
  body exceeds the configured byte cap

A fetcher MAY still wrap genuinely transient lower-level exceptions
(timeouts, connection resets, generic `httpx` errors) as a retryable
fetch-error subclass; the prohibition is specifically on collapsing a
non-retryable signal into a retryable one.

The arXiv PDF fetch path SHALL honor this rule.
A `FetchTooLargeError` raised by the egress helper SHALL surface to the job runner as `FetchTooLargeError`, not as `ArxivPdfFetchError`.

This requirement closes a classification hole on the arXiv path where
oversized PDF responses were being re-raised as a retryable error,
causing the worker to retry the same oversized fetch indefinitely
instead of failing the job permanently.

#### Scenario: Oversized arXiv PDF surfaces as FetchTooLargeError

- **GIVEN** a fetcher invocation that resolves to an arXiv PDF whose
  response body exceeds `fetch_max_response_bytes`
- **WHEN** the egress helper raises `FetchTooLargeError`
- **THEN** the arXiv fetch path re-raises `FetchTooLargeError` unchanged;
  the error is NOT wrapped as `ArxivPdfFetchError`, and the job runner
  sees `retryable = False`

#### Scenario: Egress-policy rejection on arXiv path stays non-retryable

- **GIVEN** a fetcher invocation that resolves to an arXiv URL whose
  destination is in the egress deny set
- **WHEN** the egress helper raises an `EgressPolicyError` subclass
- **THEN** the arXiv fetch path re-raises the egress error unchanged
  (existing behavior; restated to pin the broader rule)

#### Scenario: Generic transient failure on arXiv path stays retryable

- **GIVEN** a fetcher invocation that resolves to an arXiv URL where the
  egress helper raises a generic transient exception (e.g., a
  connection reset or unexpected HTTP error) that carries no
  non-retryable classification
- **WHEN** the arXiv fetch path catches the exception
- **THEN** the path MAY wrap it as `ArxivPdfFetchError` (retryable),
  preserving the existing transient-failure behavior
