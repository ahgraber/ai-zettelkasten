# Design: arxiv-fetch-too-large-retry-classification

## Context

`fetch_arxiv_pdf()` in `src/aizk/conversion/utilities/fetch_helpers.py` was written before `FetchTooLargeError` existed as a typed signal.
Its exception handling was patterned after the original "everything that isn't an egress rejection is transient" assumption:

```python
try:
    body, _headers = await egress_fetch_bytes(url, max_response_bytes=...)
    return body
except EgressPolicyError:
    raise
except Exception as exc:
    raise ArxivPdfFetchError(f"Failed to fetch arXiv PDF for {arxiv_id}: {exc}") from exc
```

When `egress_fetch_bytes` later started raising the typed `FetchTooLargeError` for response-cap overruns, the arXiv path did not get updated; the new error fell into the generic `except Exception` arm.
The other call site (`UrlFetcher._fetch_http`) was updated and the broader test suite already pins `FetchTooLargeError` propagation there.

The error hierarchy already encodes the right policy:

- `FetchError.retryable = True` (transient default)
- `FetchTooLargeError(FetchError).retryable = False` (size-cap overrun)
- `EgressPolicyError.retryable = False`
- `ArxivPdfFetchError(FetchError).retryable = True`

So the bug is purely in the wrapping: the typed non-retryable signal is silently flipped to retryable.

## Decisions

### Decision: Re-raise `FetchTooLargeError` explicitly alongside `EgressPolicyError`

**Chosen:** widen the existing `except EgressPolicyError: raise` clause to also pass `FetchTooLargeError` through unchanged.
Concretely:

```python
except (EgressPolicyError, FetchTooLargeError):
    raise
except Exception as exc:
    raise ArxivPdfFetchError(...) from exc
```

**Rationale:** the existing pattern of an explicit re-raise clause is small, readable, and parallel to the `UrlFetcher._fetch_http` shape.
Reviewers can see exactly which typed errors bypass wrapping.

**Alternatives considered:**

- Inspect `exc.retryable` reflectively and re-raise on `False`.
  Works generically, but couples the fetcher to an attribute that lives on the exception class and adds a runtime branch where a static tuple suffices.
  Save reflection for a case where the set of non-retryable errors is dynamic.
- Make `ArxivPdfFetchError.retryable` mirror its cause.
  Solves the symptom for this fetcher only and leaks classification logic into the exception class itself; the cause can be `None` after a wrap and the contract becomes harder to reason about.

---

### Decision: Restate the rule in the pluggable-pipeline spec, not the worker spec

**Chosen:** add the new requirement under `pluggable-pipeline`, adjacent to the existing egress-policy requirement.

**Rationale:** the contract is about how fetchers translate lower-level errors, which is a fetcher-protocol concern.
The worker spec already trusts whatever classification the fetcher surfaces and does not need to know the propagation rule.

## Architecture

```text
egress_fetch_bytes
    │
    ├── DenyListDestination / SchemeNotAllowed → EgressPolicyError
    ├── body > max_response_bytes              → FetchTooLargeError (retryable=False)
    └── transient httpx error                  → bubbles up

fetch_arxiv_pdf (after this change)
    │
    ├── EgressPolicyError      → re-raise unchanged
    ├── FetchTooLargeError     → re-raise unchanged   ← new
    └── other Exception        → ArxivPdfFetchError (retryable=True)

worker job runner
    │
    └── classify by exc.retryable
          ├── False → FAILED_PERM (no retry)
          └── True  → FAILED_RETRYABLE (eligible for retry)
```

## Risks

- **Other fetcher paths with the same shape.**
  A grep over `src/aizk/conversion/utilities/fetch_helpers.py` and `src/aizk/conversion/adapters/fetchers/` should be part of the work, to confirm only the arXiv path is affected.
  If any other path has the same `except Exception → wrap retryable` shape over a call that can raise `FetchTooLargeError`, it should be fixed in the same change for consistency rather than left to a follow-up.

- **Future non-retryable additions.**
  If a new non-retryable `FetchError` subclass is introduced later, the static-tuple form needs to be widened.
  The reflective alternative would scale automatically; the trade-off is accepted because the static form is clearer at the call site and new non-retryable errors are rare.
