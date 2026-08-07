"""Tests for the KaraKeep conversion backfill (``aizk.conversion.backfill``).

The backfill pages KaraKeep and submits each bookmark to the conversion API's
own submission endpoint, so the API's source materialization, idempotency key,
and queue admission all apply unchanged. These tests drive it against a stubbed
HTTP transport and a stubbed KaraKeep client: no socket is opened.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from aizk.conversion.backfill import (
    ConversionApiUnreachableError,
    preflight_api,
    resolve_conversion_api_base_url,
    run_conversion_backfill,
)


class _StubKarakeep:
    """A KaraKeep client returning canned pages, recording the cursors it was asked for."""

    def __init__(self, pages: list[tuple[list[str], str | None]]) -> None:
        """Store the ``(bookmark_ids, next_cursor)`` pages to serve in order."""
        self._pages = pages
        self.requested_cursors: list[str | None] = []

    async def get_bookmarks_paged(self, *, limit: int, cursor: str | None, include_content: bool) -> SimpleNamespace:
        """Return the next canned page, shaped like ``PaginatedBookmarks``."""
        self.requested_cursors.append(cursor)
        bookmark_ids, next_cursor = self._pages[len(self.requested_cursors) - 1]
        return SimpleNamespace(
            bookmarks=[SimpleNamespace(id=bookmark_id) for bookmark_id in bookmark_ids],
            next_cursor=next_cursor,
        )


def _client(handler) -> httpx.AsyncClient:
    """Build an AsyncClient whose requests are served by ``handler`` in-process."""
    return httpx.AsyncClient(base_url="http://api.test", transport=httpx.MockTransport(handler))


# --- base URL resolution ----------------------------------------------------


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("0.0.0.0", "http://127.0.0.1:8000"),  # noqa: S104 - the bind-address default being translated
        ("127.0.0.1", "http://127.0.0.1:8000"),
        ("api.internal", "http://api.internal:8000"),
        ("::", "http://[::1]:8000"),
        ("::1", "http://[::1]:8000"),
        ("fd00::1", "http://[fd00::1]:8000"),
    ],
)
def test_resolve_conversion_api_base_url_translates_the_bind_address(host: str, expected: str) -> None:
    """A bind-all address resolves to the matching loopback in its own address family.

    ``0.0.0.0`` and ``::`` mean "listen on every interface" and are not dialable,
    so each maps to loopback without crossing address families — mapping ``::``
    to ``127.0.0.1`` would miss an IPv6-only listener.
    """
    config = SimpleNamespace(api_host=host, api_port=8000)
    assert resolve_conversion_api_base_url(config) == expected


@pytest.mark.parametrize("host", ["::1", "::", "fd00::1", "127.0.0.1", "api.internal"])
def test_resolve_conversion_api_base_url_produces_a_url_httpx_accepts(host: str) -> None:
    """Every resolved base URL parses, so an IPv6 host does not fail at client construction."""
    url = resolve_conversion_api_base_url(SimpleNamespace(api_host=host, api_port=8000))
    assert httpx.URL(url).host


# --- preflight --------------------------------------------------------------


@pytest.mark.asyncio
async def test_preflight_api_accepts_a_reachable_api() -> None:
    """A responding readiness endpoint satisfies the preflight."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/health/ready"
        return httpx.Response(200, json={"status": "ok"})

    async with _client(handler) as client:
        await preflight_api(client)


@pytest.mark.asyncio
async def test_preflight_api_reports_an_unreachable_api_with_the_command_to_start_it() -> None:
    """A refused connection is reported as a typed error naming the command that fixes it."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with _client(handler) as client:
        with pytest.raises(ConversionApiUnreachableError, match="aizk-conversion serve"):
            await preflight_api(client)


@pytest.mark.asyncio
async def test_preflight_api_rejects_an_api_that_is_not_ready() -> None:
    """A 503 means a required dependency is down, so submissions would fail across the corpus.

    ``/health/ready`` returns 503 when the database check fails, and every
    submission writes database rows. Paging all of KaraKeep to collect failures
    is worse than refusing up front.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"status": "unavailable", "checks": [{"name": "db", "status": "error"}]})

    async with _client(handler) as client:
        with pytest.raises(ConversionApiUnreachableError, match="not ready"):
            await preflight_api(client)


@pytest.mark.asyncio
async def test_preflight_api_rejects_a_missing_readiness_endpoint() -> None:
    """A 404 proves something answered, not that the conversion API did; it does not pass."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="Not Found")

    async with _client(handler) as client:
        with pytest.raises(ConversionApiUnreachableError):
            await preflight_api(client)


@pytest.mark.asyncio
async def test_preflight_api_surfaces_the_failing_readiness_check() -> None:
    """The refusal names which dependency failed, so the operator knows what to fix."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            json={"status": "unavailable", "checks": [{"name": "s3", "status": "error", "detail": "timeout"}]},
        )

    async with _client(handler) as client:
        with pytest.raises(ConversionApiUnreachableError, match="s3"):
            await preflight_api(client)


# --- the backfill run -------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_pages_through_karakeep_and_submits_every_bookmark() -> None:
    """Every bookmark on every page is submitted, following the cursor to exhaustion."""
    karakeep = _StubKarakeep([(["b1", "b2"], "cursor-2"), (["b3"], None)])
    submitted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health/ready":
            return httpx.Response(200, json={})
        submitted.append(request.url.path)
        return httpx.Response(201, json={"id": len(submitted)})

    async with _client(handler) as client:
        result = await run_conversion_backfill(
            http_client=client, karakeep_client=karakeep, page_size=100, limit=None, dry_run=False
        )

    assert (result.submitted, result.existing, result.failed) == (3, 0, 0)
    assert karakeep.requested_cursors == [None, "cursor-2"]
    assert len(submitted) == 3


@pytest.mark.asyncio
async def test_backfill_counts_a_deduped_submission_separately_from_a_new_one() -> None:
    """The API's 200-on-duplicate is reported as reused, not as a fresh submission."""
    karakeep = _StubKarakeep([(["b1", "b2"], None)])
    statuses = iter([201, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health/ready":
            return httpx.Response(200, json={})
        return httpx.Response(next(statuses), json={"id": 1})

    async with _client(handler) as client:
        result = await run_conversion_backfill(
            http_client=client, karakeep_client=karakeep, page_size=100, limit=None, dry_run=False
        )

    assert (result.submitted, result.existing, result.failed) == (1, 1, 0)


@pytest.mark.asyncio
async def test_backfill_honors_retry_after_when_the_queue_is_full() -> None:
    """A 503 is retried after the server's Retry-After delay rather than hammered or dropped."""
    karakeep = _StubKarakeep([(["b1"], None)])
    attempts: list[str] = []
    slept: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health/ready":
            return httpx.Response(200, json={})
        attempts.append("submit")
        if len(attempts) == 1:
            return httpx.Response(503, json={"detail": "Queue is at capacity"}, headers={"Retry-After": "7"})
        return httpx.Response(201, json={"id": 1})

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    async with _client(handler) as client:
        result = await run_conversion_backfill(
            http_client=client,
            karakeep_client=karakeep,
            page_size=100,
            limit=None,
            dry_run=False,
            sleep=fake_sleep,
        )

    assert slept == [7.0], "the server's Retry-After delay is respected verbatim"
    assert (result.submitted, result.failed) == (1, 0)


@pytest.mark.asyncio
async def test_backfill_does_not_sleep_after_its_final_attempt() -> None:
    """When retries run out, the run reports the failure instead of waiting for nothing.

    A sleep after the last attempt delays the result by the server's Retry-After
    — up to the fallback delay — without another request ever following it.
    """
    karakeep = _StubKarakeep([(["b1"], None)])
    attempts: list[str] = []
    slept: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health/ready":
            return httpx.Response(200, json={"status": "ok"})
        attempts.append("submit")
        return httpx.Response(503, json={"detail": "full"}, headers={"Retry-After": "7"})

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    async with _client(handler) as client:
        result = await run_conversion_backfill(
            http_client=client,
            karakeep_client=karakeep,
            page_size=100,
            limit=None,
            dry_run=False,
            sleep=fake_sleep,
        )

    assert len(slept) == len(attempts) - 1, "each sleep precedes a retry; the last attempt is followed by none"
    assert result.failed == 1, "an exhausted retry budget is a failed bookmark, not a silent success"


@pytest.mark.asyncio
async def test_backfill_counts_a_rejected_bookmark_without_aborting_the_run() -> None:
    """One unacceptable bookmark is counted as failed; the rest of the corpus still submits."""
    karakeep = _StubKarakeep([(["bad", "good"], None)])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health/ready":
            return httpx.Response(200, json={})
        if "bad" in request.content.decode():
            return httpx.Response(422, json={"detail": {"error": "kind_not_accepted"}})
        return httpx.Response(201, json={"id": 1})

    async with _client(handler) as client:
        result = await run_conversion_backfill(
            http_client=client, karakeep_client=karakeep, page_size=100, limit=None, dry_run=False
        )

    assert (result.submitted, result.failed) == (1, 1)


@pytest.mark.asyncio
async def test_backfill_limit_stops_after_the_requested_bookmark_count() -> None:
    """``limit`` bounds total submissions, stopping mid-page rather than finishing it."""
    karakeep = _StubKarakeep([(["b1", "b2", "b3"], "cursor-2"), (["b4"], None)])
    submitted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health/ready":
            return httpx.Response(200, json={})
        submitted.append(request.url.path)
        return httpx.Response(201, json={"id": len(submitted)})

    async with _client(handler) as client:
        result = await run_conversion_backfill(
            http_client=client, karakeep_client=karakeep, page_size=100, limit=2, dry_run=False
        )

    assert result.submitted == 2
    assert len(submitted) == 2
    assert karakeep.requested_cursors == [None], "the run stops without fetching a second page"


@pytest.mark.asyncio
async def test_backfill_dry_run_submits_nothing() -> None:
    """A dry run pages KaraKeep and reports the count without POSTing a single job."""
    karakeep = _StubKarakeep([(["b1", "b2"], None)])
    submitted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health/ready":
            return httpx.Response(200, json={})
        submitted.append(request.url.path)
        return httpx.Response(201, json={"id": 1})

    async with _client(handler) as client:
        result = await run_conversion_backfill(
            http_client=client, karakeep_client=karakeep, page_size=100, limit=None, dry_run=True
        )

    assert result.submitted == 2, "a dry run reports what it would submit"
    assert submitted == [], "a dry run must not POST"


@pytest.mark.asyncio
async def test_backfill_preflights_before_paging_karakeep() -> None:
    """An unreachable API fails before any KaraKeep page is fetched."""
    karakeep = _StubKarakeep([(["b1"], None)])

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with _client(handler) as client:
        with pytest.raises(ConversionApiUnreachableError):
            await run_conversion_backfill(
                http_client=client, karakeep_client=karakeep, page_size=100, limit=None, dry_run=False
            )

    assert karakeep.requested_cursors == [], "KaraKeep is not paged when the API is down"
