"""In-process httpx2 transports serving canned responses."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

import httpx2

from tests.fakes import FIXTURES_DIR

if TYPE_CHECKING:
    import pytest

    from aizk.conversion.egress.policy import ValidatedDestination

KARAKEEP_BASE_URL = "http://karakeep.test"
KARAKEEP_BOOKMARK_ID = "bm_fixture_article"
ARTICLE_URL = "https://article.test/posts/analytical-engine"
ARXIV_ID = "2301.12345"
GITHUB_OWNER = "fixture-org"
GITHUB_REPO = "fixture-repo"

_ROUTES: dict[tuple[str, str], tuple[str, str]] = {
    ("karakeep.test", f"/api/v1/bookmarks/{KARAKEEP_BOOKMARK_ID}"): ("karakeep_bookmark.json", "application/json"),
    ("article.test", "/posts/analytical-engine"): ("article.html", "text/html; charset=utf-8"),
    ("export.arxiv.org", f"/pdf/{ARXIV_ID}"): ("arxiv.pdf", "application/pdf"),
    ("arxiv.org", f"/pdf/{ARXIV_ID}"): ("arxiv.pdf", "application/pdf"),
    ("raw.githubusercontent.com", f"/{GITHUB_OWNER}/{GITHUB_REPO}/main/README.md"): (
        "README.md",
        "text/plain; charset=utf-8",
    ),
}


def mock_transport_factory(
    handler: Callable[[httpx2.Request], httpx2.Response],
    connected_to: list[str] | None = None,
) -> Callable[["ValidatedDestination"], httpx2.AsyncBaseTransport]:
    """Return a transport factory backed by an httpx2.MockTransport; ``connected_to`` collects each destination IP."""

    def factory(destination: "ValidatedDestination") -> httpx2.AsyncBaseTransport:
        if connected_to is not None:
            connected_to.append(destination.ip)
        return httpx2.MockTransport(handler)

    return factory


def mock_async_client(
    handler: Callable[[httpx2.Request], httpx2.Response],
    *,
    base_url: str = "http://api.test",
) -> httpx2.AsyncClient:
    """Build an AsyncClient whose requests are served by ``handler`` in-process."""
    return httpx2.AsyncClient(base_url=base_url, transport=httpx2.MockTransport(handler))


def fixtured_handler() -> Callable[[httpx2.Request], httpx2.Response]:
    """Return a handler that serves the fixture files by request host and path; unknown routes get 404."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        route = _ROUTES.get((request.url.host, request.url.path))
        if route is None:
            return httpx2.Response(404)
        filename, content_type = route
        return httpx2.Response(
            200, headers={"content-type": content_type}, content=(FIXTURES_DIR / filename).read_bytes()
        )

    return handler


def install_fixtured_egress(monkeypatch: "pytest.MonkeyPatch") -> None:
    """Serve every egress fetch and every KaraKeep API call from the fixture files, in-process."""
    from aizk.conversion.egress.fetch import egress_fetch_bytes

    handler = fixtured_handler()
    factory = mock_transport_factory(handler)

    async def _fixtured_fetch_bytes(url: str, **kwargs):
        return await egress_fetch_bytes(url, transport_factory=factory, **kwargs)

    def _ensure_client(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.MockTransport(handler), timeout=self.timeout)

    for module in (
        "aizk.conversion.adapters.fetchers.url",
        "aizk.conversion.adapters.fetchers.arxiv",
        "aizk.conversion.adapters.fetchers.github",
        "aizk.conversion.egress.fetch_helpers",
    ):
        monkeypatch.setattr(f"{module}.egress_fetch_bytes", _fixtured_fetch_bytes)
    monkeypatch.setattr("karakeep_client.karakeep.KarakeepClient._ensure_client", _ensure_client)
