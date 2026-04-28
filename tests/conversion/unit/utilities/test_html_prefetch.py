"""Unit tests for the HTML <img> pre-fetch helper."""

from __future__ import annotations

import hashlib
from pathlib import Path

import lxml.html
import pytest

from aizk.conversion.core.errors import DenyListDestination, FetchTooLargeError
from aizk.conversion.utilities import html_prefetch
from aizk.conversion.utilities.html_prefetch import (
    PrefetchPolicy,
    _extension_for_content_type,
    prefetch_images,
)

# --- Content-Type → extension --------------------------------------------------


@pytest.mark.parametrize(
    "ct, expected",
    [
        ("image/png", ".png"),
        ("image/jpeg", ".jpg"),
        ("image/gif", ".gif"),
        ("image/webp", ".webp"),
        ("image/svg+xml", ".svg"),
        ("image/png; charset=utf-8", ".png"),
        ("IMAGE/PNG", ".png"),
        ("text/html", ".bin"),
        ("application/octet-stream", ".bin"),
        ("", ".bin"),
        (None, ".bin"),
    ],
)
def test_extension_for_content_type_uses_fixed_allowlist(ct: str | None, expected: str) -> None:
    assert _extension_for_content_type(ct) == expected


# --- Helpers -----------------------------------------------------------------


def _patch_fetch(monkeypatch: pytest.MonkeyPatch, responses: dict[str, tuple[bytes, dict[str, str]]]) -> list[str]:
    """Stub `egress_fetch_bytes` to return the configured response per src; return call log."""
    called: list[str] = []

    async def fake(url: str, **_kwargs: object) -> tuple[bytes, dict[str, str]]:
        called.append(url)
        if url not in responses:
            raise AssertionError(f"unexpected fetch: {url}")
        return responses[url]

    monkeypatch.setattr(html_prefetch, "egress_fetch_bytes", fake)
    return called


# --- Happy path --------------------------------------------------------------


@pytest.mark.asyncio
async def test_rewrites_src_to_absolute_workspace_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    payload = b"\x89PNGsmall"
    _patch_fetch(monkeypatch, {"https://cdn.example/a.png": (payload, {"content-type": "image/png"})})

    html = '<html><body><img src="https://cdn.example/a.png"></body></html>'
    rewritten = await prefetch_images(html, tmp_path)

    doc = lxml.html.fromstring(rewritten)
    img = doc.find(".//img")
    assert img is not None
    new_src = img.get("src")
    assert new_src is not None
    assert new_src.startswith(str(tmp_path.resolve()))
    saved_path = Path(new_src)
    assert saved_path.read_bytes() == payload
    expected_name = f"{hashlib.sha256(payload).hexdigest()}.png"
    assert saved_path.name == expected_name


@pytest.mark.asyncio
async def test_data_url_passthrough(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`data:` URLs must NOT be fetched and must remain unmodified in the HTML."""
    called = _patch_fetch(monkeypatch, {})

    html = '<html><body><img src="data:image/png;base64,AAAA"></body></html>'
    rewritten = await prefetch_images(html, tmp_path)

    assert called == []
    doc = lxml.html.fromstring(rewritten)
    assert doc.find(".//img").get("src") == "data:image/png;base64,AAAA"


# --- Per-image failures ------------------------------------------------------


@pytest.mark.asyncio
async def test_egress_violation_leaves_src_unmodified(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A typed egress error skips just that image; conversion of the rest proceeds."""
    payload = b"OK"

    async def fake(url: str, **_kwargs: object) -> tuple[bytes, dict[str, str]]:
        if "169.254.169.254" in url:
            raise DenyListDestination("denied")
        return payload, {"content-type": "image/png"}

    monkeypatch.setattr(html_prefetch, "egress_fetch_bytes", fake)

    html = (
        '<html><body><img src="http://169.254.169.254/secret.png"><img src="https://cdn.example/ok.png"></body></html>'
    )
    rewritten = await prefetch_images(html, tmp_path)
    doc = lxml.html.fromstring(rewritten)
    imgs = doc.findall(".//img")
    # First img: deny-set src left as-is for Docling/workspace gate to reject.
    assert imgs[0].get("src") == "http://169.254.169.254/secret.png"
    # Second img: rewritten to local workspace path.
    assert imgs[1].get("src", "").startswith(str(tmp_path.resolve()))


@pytest.mark.asyncio
async def test_size_overrun_skips_image(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A FetchTooLargeError leaves that <img src> unchanged."""

    async def fake(url: str, **_kwargs: object) -> tuple[bytes, dict[str, str]]:
        raise FetchTooLargeError("over cap")

    monkeypatch.setattr(html_prefetch, "egress_fetch_bytes", fake)

    html = '<html><body><img src="https://cdn.example/big.png"></body></html>'
    rewritten = await prefetch_images(html, tmp_path)
    doc = lxml.html.fromstring(rewritten)
    assert doc.find(".//img").get("src") == "https://cdn.example/big.png"


# --- Per-document cap --------------------------------------------------------


@pytest.mark.asyncio
async def test_per_document_image_count_cap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """With 51 <img> tags, the first 50 are rewritten and the 51st keeps its src."""
    payload = b"\x89PNGblob"
    fetched_urls: list[str] = []

    async def fake(url: str, **_kwargs: object) -> tuple[bytes, dict[str, str]]:
        fetched_urls.append(url)
        return payload, {"content-type": "image/png"}

    monkeypatch.setattr(html_prefetch, "egress_fetch_bytes", fake)

    imgs_html = "".join(f'<img src="https://cdn.example/{i}.png">' for i in range(51))
    html = f"<html><body>{imgs_html}</body></html>"
    # Relax the per-host cap so this test isolates the document-level total
    # cap (the per-host cap is exercised by `test_per_host_image_cap_*`).
    rewritten = await prefetch_images(html, tmp_path, policy=PrefetchPolicy(max_images_per_host=100))

    # Helper called exactly 50 times — 51st never fetched.
    assert len(fetched_urls) == 50

    doc = lxml.html.fromstring(rewritten)
    imgs = doc.findall(".//img")
    rewritten_count = sum(1 for img in imgs if img.get("src", "").startswith(str(tmp_path.resolve())))
    untouched_count = sum(1 for img in imgs if img.get("src", "").startswith("https://cdn.example/"))
    assert rewritten_count == 50
    assert untouched_count == 1


# --- Per-host cap ------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_host_image_cap_drops_images_beyond_cap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Many <img> tags pointing at one host are capped at PrefetchPolicy.max_images_per_host.

    Defends against an HTML doc fanning out 50 <img> tags at one victim host
    (outbound-amplification primitive). The first ``max_images_per_host``
    images for that host get prefetched; the rest are left as-is so
    ``enable_remote_fetch=False`` blocks them at the converter.
    """
    payload = b"\x89PNGdata"
    fetched_urls: list[str] = []

    async def fake(url: str, **_kwargs: object) -> tuple[bytes, dict[str, str]]:
        fetched_urls.append(url)
        return payload, {"content-type": "image/png"}

    monkeypatch.setattr(html_prefetch, "egress_fetch_bytes", fake)

    # 15 images, all at the same host. Cap of 5 → only first 5 fetched.
    imgs_html = "".join(f'<img src="https://victim.example/{i}.png">' for i in range(15))
    html = f"<html><body>{imgs_html}</body></html>"
    rewritten = await prefetch_images(html, tmp_path, policy=PrefetchPolicy(max_images_per_host=5))

    assert len(fetched_urls) == 5

    doc = lxml.html.fromstring(rewritten)
    imgs = doc.findall(".//img")
    rewritten_count = sum(1 for img in imgs if img.get("src", "").startswith(str(tmp_path.resolve())))
    untouched_count = sum(1 for img in imgs if img.get("src", "").startswith("https://victim.example/"))
    assert rewritten_count == 5
    assert untouched_count == 10


@pytest.mark.asyncio
async def test_per_host_cap_independent_across_hosts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The per-host cap counts each hostname separately."""
    payload = b"\x89PNGdata"
    fetched_urls: list[str] = []

    async def fake(url: str, **_kwargs: object) -> tuple[bytes, dict[str, str]]:
        fetched_urls.append(url)
        return payload, {"content-type": "image/png"}

    monkeypatch.setattr(html_prefetch, "egress_fetch_bytes", fake)

    # 6 images split across 2 hosts; cap of 3 per host → all 6 fetched.
    imgs_html = "".join(f'<img src="https://a.example/{i}.png">' for i in range(3)) + "".join(
        f'<img src="https://b.example/{i}.png">' for i in range(3)
    )
    html = f"<html><body>{imgs_html}</body></html>"
    rewritten = await prefetch_images(html, tmp_path, policy=PrefetchPolicy(max_images_per_host=3))

    assert len(fetched_urls) == 6
    doc = lxml.html.fromstring(rewritten)
    rewritten_count = sum(1 for img in doc.findall(".//img") if img.get("src", "").startswith(str(tmp_path.resolve())))
    assert rewritten_count == 6


# --- Content-Type-derived extension ------------------------------------------


@pytest.mark.asyncio
async def test_lying_content_type_falls_back_to_bin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A server returning text/html for <img> data should produce a .bin extension."""
    payload = b"<html>fake</html>"
    _patch_fetch(monkeypatch, {"https://cdn.example/x.png": (payload, {"content-type": "text/html"})})

    html = '<html><body><img src="https://cdn.example/x.png"></body></html>'
    rewritten = await prefetch_images(html, tmp_path)
    doc = lxml.html.fromstring(rewritten)
    new_src = doc.find(".//img").get("src")
    assert new_src is not None
    assert new_src.endswith(".bin")
    assert Path(new_src).read_bytes() == payload
