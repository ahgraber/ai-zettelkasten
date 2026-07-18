"""Integration tests for operator console reachability and cross-page navigation.

Drives the real operator console app (over the shared migration-built SQLite
harness) to assert the operator-facing navigation contracts the individual page
tests do not cover: the app root reaches the console, and every full-page console
surface carries the shared nav that links to the others and marks the current
section as active.
"""

from __future__ import annotations

import pytest

from fastapi.testclient import TestClient

#: The full-page console surfaces and each one's own nav link, paired so the
#: active-state assertion can name the current section per surface.
_PAGES = [
    ("/ui", "/ui"),
    ("/ui/tasks", "/ui/tasks"),
    ("/ui/explore/chunks", "/ui/explore/chunks"),
]
#: Every console section linked from the nav; each full page must link to all.
_NAV_LINKS = ["/ui", "/ui/tasks", "/ui/explore/chunks"]


def test_root_redirects_to_console_dashboard(client: TestClient) -> None:
    """The bare app root redirects (307) to the operator console dashboard."""
    response = client.get("/", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/ui"


@pytest.mark.parametrize("path", [page for page, _ in _PAGES])
def test_page_links_to_every_console_section(client: TestClient, path: str) -> None:
    """Each full page renders the shared nav linking to all console sections."""
    body = client.get(path).text

    assert 'class="nav"' in body
    for link in _NAV_LINKS:
        assert f'href="{link}"' in body


@pytest.mark.parametrize(("path", "own_link"), _PAGES)
def test_current_section_is_marked_active(client: TestClient, path: str, own_link: str) -> None:
    """The nav marks only the current section's link as ``aria-current="page"``."""
    body = client.get(path).text

    assert f'href="{own_link}" aria-current="page">' in body
    # Count the anchor-tag form only; the nav's CSS selector also contains the
    # attribute but never the closing ``>``.
    assert body.count('aria-current="page">') == 1


def test_htmx_panel_partial_omits_nav(client: TestClient) -> None:
    """An ``HX-Request`` returns the inner panel only, so a swap never duplicates the nav."""
    body = client.get("/ui/tasks", headers={"HX-Request": "true"}).text

    assert 'class="nav"' not in body
