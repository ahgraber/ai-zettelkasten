"""KaraKeep corpus backfill for the conversion stage.

Pages KaraKeep and submits each bookmark to the conversion API's own submission
endpoint rather than writing to the database. That endpoint owns source
materialization, the idempotency key derived from the live converter
configuration, and queue admission — reimplementing any of it here would fork a
boundary contract and let the two copies drift. Submitting through it also means
a repeated backfill is cheap: the API answers a duplicate with ``200`` and the
existing job instead of creating a second one.

Queue admission is the one thing a bulk submitter must respect. When the queue is
at capacity the API answers ``503`` with a ``Retry-After`` header; this waits that
long and retries rather than dropping the bookmark or hammering the endpoint.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any, Protocol

import httpx

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

_READINESS_PATH = "/health/ready"
_JOBS_PATH = "/v1/jobs"
_DEFAULT_PAGE_SIZE = 100
# Bounds a pathological retry loop against an API that never drains; each wait is
# the server's own Retry-After, so this caps attempts, not total patience.
_MAX_QUEUE_FULL_RETRIES = 10
_FALLBACK_RETRY_AFTER_SECONDS = 30.0


class ConversionApiUnreachableError(RuntimeError):
    """The conversion API did not answer, so there is nothing to submit bookmarks to."""


class BookmarkPager(Protocol):
    """The one KaraKeep capability a backfill needs: cursor-paged bookmark listing."""

    async def get_bookmarks_paged(self, *, limit: int, cursor: str | None, include_content: bool) -> Any:  # noqa: ANN401 - the client's own paginated model, matched structurally
        """Return one page of bookmarks and the cursor for the next."""
        ...


@dataclass(frozen=True)
class ConversionBackfillResult:
    """How a conversion backfill run resolved and submitted its bookmarks.

    Attributes:
        submitted: Bookmarks the API accepted as new conversion jobs.
        existing: Bookmarks that deduped onto an existing job.
        failed: Bookmarks the API rejected or that errored in transit.
    """

    submitted: int
    existing: int
    failed: int


def resolve_conversion_api_base_url(config: Any) -> str:  # noqa: ANN401 - ConversionConfig, kept loose for testability
    """Return the base URL a client should dial for the conversion API.

    ``api_host`` is a bind address. ``0.0.0.0`` and ``::`` mean "listen on every
    interface" and are not dialable, so each resolves to the loopback address of
    its own family — mapping ``::`` to an IPv4 loopback would miss an IPv6-only
    listener. IPv6 literals are bracketed so the result is a valid URL.
    """
    host = config.api_host
    if host == "0.0.0.0":  # noqa: S104 - translating the bind-all default, not binding to it
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{config.api_port}"


def _failing_checks(response: httpx.Response) -> str:
    """Summarize the readiness checks that did not pass, for the refusal message."""
    try:
        checks = response.json().get("checks") or []
    except ValueError:
        return f"HTTP {response.status_code}"
    failed = [check.get("name", "?") for check in checks if check.get("status") != "ok"]
    return ", ".join(failed) if failed else f"HTTP {response.status_code}"


async def preflight_api(http_client: httpx.AsyncClient) -> None:
    """Verify the conversion API is ready before a backfill starts paging KaraKeep.

    Readiness, not mere reachability, is the bar. Every submission writes database
    rows, so an API whose database check is failing cannot accept them; paging all
    of KaraKeep to collect a corpus of failures is worse than refusing up front.
    Any non-``200`` answer is therefore a refusal, which also catches a wrong path
    answering ``404``.

    Raises:
        ConversionApiUnreachableError: If the API cannot be reached, or reports
            itself not ready.
    """
    try:
        response = await http_client.get(_READINESS_PATH)
    except httpx.RequestError as exc:
        raise ConversionApiUnreachableError(
            f"conversion API is not reachable at {http_client.base_url} — "
            "start it with `aizk-conversion serve` (or `just serve`)."
        ) from exc
    if response.status_code != httpx.codes.OK:
        raise ConversionApiUnreachableError(
            f"conversion API at {http_client.base_url} is not ready ({_failing_checks(response)}) — "
            "submissions would fail, so the backfill will not start."
        )


async def _submit_bookmark(
    http_client: httpx.AsyncClient,
    bookmark_id: str,
    *,
    sleep: "Callable[[float], Awaitable[None]]",
) -> httpx.Response:
    """Submit one bookmark, waiting out queue-full responses.

    Returns the API's final response. A ``503`` carries the server's own
    ``Retry-After``; that delay is honored verbatim and the submission retried,
    up to a bounded number of attempts. Each wait precedes a retry, so the final
    attempt's ``503`` is reported immediately rather than after a delay no
    request follows.
    """
    payload = {"source_ref": {"kind": "karakeep_bookmark", "bookmark_id": bookmark_id}}
    for attempt in range(_MAX_QUEUE_FULL_RETRIES):
        response = await http_client.post(_JOBS_PATH, json=payload)
        if response.status_code != httpx.codes.SERVICE_UNAVAILABLE:
            return response
        if attempt == _MAX_QUEUE_FULL_RETRIES - 1:
            break
        retry_after = response.headers.get("Retry-After")
        delay = float(retry_after) if retry_after and retry_after.isdigit() else _FALLBACK_RETRY_AFTER_SECONDS
        logger.info(
            "Conversion queue is at capacity; waiting before retry",
            extra={"bookmark_id": bookmark_id, "retry_after_seconds": delay},
        )
        await sleep(delay)
    logger.warning(
        "Conversion queue stayed at capacity for every attempt",
        extra={"bookmark_id": bookmark_id, "attempts": _MAX_QUEUE_FULL_RETRIES},
    )
    return response


async def run_conversion_backfill(
    *,
    http_client: httpx.AsyncClient,
    karakeep_client: BookmarkPager,
    page_size: int = _DEFAULT_PAGE_SIZE,
    limit: int | None = None,
    dry_run: bool = False,
    sleep: "Callable[[float], Awaitable[None]] | None" = None,
) -> ConversionBackfillResult:
    """Page KaraKeep and submit every bookmark to the conversion API.

    Preflights the API before fetching anything, so an unreachable service fails
    immediately rather than after a full corpus walk. A rejected bookmark is
    counted and the run continues; only an unreachable API aborts it.

    Args:
        http_client: Client based at the conversion API; injected so callers share
            its timeouts and instrumentation.
        karakeep_client: The paged bookmark source.
        page_size: KaraKeep page size (its maximum is 100).
        limit: Stop after submitting this many bookmarks, mid-page if need be.
        dry_run: Page and count without submitting anything.
        sleep: Delay function used for queue-full backoff; defaults to
            :func:`asyncio.sleep` and is injected by tests.

    Returns:
        The run's :class:`ConversionBackfillResult`.

    Raises:
        ConversionApiUnreachableError: If the API cannot be reached.
    """
    wait = sleep if sleep is not None else asyncio.sleep
    await preflight_api(http_client)

    submitted = existing = failed = 0
    seen = 0
    cursor: str | None = None

    while True:
        page = await karakeep_client.get_bookmarks_paged(limit=page_size, cursor=cursor, include_content=False)
        for bookmark in page.bookmarks:
            if limit is not None and seen >= limit:
                break
            seen += 1
            if dry_run:
                submitted += 1
                continue
            try:
                response = await _submit_bookmark(http_client, bookmark.id, sleep=wait)
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                failed += 1
                logger.warning(
                    "Bookmark submission rejected",
                    extra={"bookmark_id": bookmark.id, "status_code": exc.response.status_code},
                )
            except httpx.HTTPError:
                failed += 1
                logger.warning("Bookmark submission failed in transit", extra={"bookmark_id": bookmark.id})
            else:
                # The API answers a duplicate with 200 and the existing job; only a
                # 201 is a job this run caused to exist.
                if response.status_code == httpx.codes.CREATED:
                    submitted += 1
                else:
                    existing += 1

        if limit is not None and seen >= limit:
            break
        if not page.next_cursor:
            break
        cursor = page.next_cursor

    result = ConversionBackfillResult(submitted=submitted, existing=existing, failed=failed)
    logger.info(
        "Conversion backfill complete",
        extra={
            "stage": "conversion",
            "submitted": result.submitted,
            "existing": result.existing,
            "failed": result.failed,
            "dry_run": dry_run,
        },
    )
    return result
