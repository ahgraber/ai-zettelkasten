"""Integration tests for graph operator UI reachability and cross-page navigation.

Drives the real graph operator app (over the shared migration-built SQLite
harness) to assert two operator-facing contracts the individual page tests do
not cover: the app root reaches an operator page, and every full-page operator
surface carries the shared nav that links to the others and marks the current
page as active.
"""

from __future__ import annotations

import pytest

from fastapi.testclient import TestClient

#: The full-page operator surfaces and each one's own nav link, paired so the
#: active-state assertion can name the current page per surface.
_PAGES = [
    ("/ui/graph/jobs", "/ui/graph/jobs"),
    ("/ui/graph/extraction-jobs", "/ui/graph/extraction-jobs"),
    ("/ui/graph/explorer", "/ui/graph/explorer"),
]
#: Every operator page linked from the nav; each full page must link to all.
_NAV_LINKS = ["/ui/graph/jobs", "/ui/graph/extraction-jobs", "/ui/graph/explorer"]


def test_root_redirects_to_console_dashboard(client: TestClient) -> None:
    """The bare app root redirects (307) to the operator console dashboard."""
    response = client.get("/", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/ui"


@pytest.mark.parametrize("path", [page for page, _ in _PAGES])
def test_page_links_to_every_operator_surface(client: TestClient, path: str) -> None:
    """Each full page renders the shared nav linking to all operator surfaces."""
    body = client.get(path).text

    assert 'class="nav"' in body
    for link in _NAV_LINKS:
        assert f'href="{link}"' in body


@pytest.mark.parametrize(("path", "own_link"), _PAGES)
def test_current_page_is_marked_active(client: TestClient, path: str, own_link: str) -> None:
    """The nav marks only the current page's link as ``aria-current="page"``."""
    body = client.get(path).text

    assert f'href="{own_link}" aria-current="page">' in body
    # Count the anchor-tag form only; the nav's CSS selector also contains the
    # attribute but never the closing ``>``.
    assert body.count('aria-current="page">') == 1


def test_htmx_panel_partial_omits_nav(client: TestClient) -> None:
    """An ``HX-Request`` returns the inner panel only, so a swap never duplicates the nav."""
    body = client.get("/ui/graph/jobs", headers={"HX-Request": "true"}).text

    assert 'class="nav"' not in body
