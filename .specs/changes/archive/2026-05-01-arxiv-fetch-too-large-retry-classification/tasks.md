# Tasks: arxiv-fetch-too-large-retry-classification

## Audit

- [x] Grep `src/aizk/conversion/utilities/fetch_helpers.py` and
  `src/aizk/conversion/adapters/fetchers/` for the
  `except EgressPolicyError: raise` / `except Exception → wrap` shape
  over a call that can raise `FetchTooLargeError`; record any
  additional offenders so they can be fixed in this change rather than
  a follow-up

  **Result:** only `fetch_arxiv_pdf` exhibits the wrap-non-retryable-as-retryable
  shape. `UrlFetcher.fetch` already catches `FetchError` (parent of
  `FetchTooLargeError`) before its generic `Exception` arm.
  `GitHubFetcher` swallows `FetchError` to try the next README variant
  (intentional fallback, different shape — out of scope here).
  `fetch_karakeep_asset` does not call `egress_fetch_bytes` so
  `FetchTooLargeError` cannot originate there.

## Implementation

- [x] Update `fetch_arxiv_pdf()` in
  `src/aizk/conversion/utilities/fetch_helpers.py` so the propagation
  clause is `except (EgressPolicyError, FetchTooLargeError): raise`,
  and update the docstring's `Raises:` block to name
  `FetchTooLargeError` as a non-retryable surface error
- [x] Apply the same fix to any additional offenders found in the audit
  step (none found)

## Tests

- [x] Add a unit test in
  `tests/conversion/unit/utilities/test_fetch_helpers.py` (create if
  it does not exist) that monkeypatches `egress_fetch_bytes` to raise
  `FetchTooLargeError` and asserts `fetch_arxiv_pdf` re-raises
  `FetchTooLargeError` (not `ArxivPdfFetchError`)
- [x] Add a unit test asserting an `EgressPolicyError` subclass
  (e.g., `DenyListDestination`) propagates unchanged from
  `fetch_arxiv_pdf`
- [x] Add a unit test asserting a generic transient exception (e.g.,
  `RuntimeError("boom")`) is wrapped as `ArxivPdfFetchError`, pinning
  the retryable-default path

## Verification

- [x] Run targeted tests with
  `uv run pytest tests/conversion/unit/utilities/test_fetch_helpers.py tests/conversion/unit/adapters/fetchers/`
- [x] Run the full conversion suite with `uv run pytest tests/conversion/`
  to confirm no regressions in worker classification or other fetcher paths
