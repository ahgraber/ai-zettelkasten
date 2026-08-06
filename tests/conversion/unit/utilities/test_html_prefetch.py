"""Unit tests for the HTML <img> admission helper."""

from __future__ import annotations

import base64
import hashlib
import logging
from pathlib import Path
import socket

import lxml.html
import pytest

from aizk.conversion.core.errors import DenyListDestination, FetchTooLargeError
from aizk.conversion.utilities import html_prefetch
from aizk.conversion.utilities.html_prefetch import (
    PrefetchPolicy,
    _extension_for_content_type,
    _media_type_for_content_type,
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


# --- Content-Type → data: URI media type ---------------------------------------


@pytest.mark.parametrize(
    "ct, expected",
    [
        ("image/png", "image/png"),
        ("image/jpeg", "image/jpeg"),
        ("image/gif", "image/gif"),
        ("image/webp", "image/webp"),
        ("image/svg+xml", "image/svg+xml"),
        ("image/png; charset=utf-8", "image/png"),
        ("IMAGE/PNG", "image/png"),
        # Outside the allowlist and absent both fall back to a generic image
        # media type so the converter still decodes and sniffs the payload.
        ("text/html", "image/octet-stream"),
        ("application/octet-stream", "image/octet-stream"),
        ("", "image/octet-stream"),
        (None, "image/octet-stream"),
    ],
)
def test_media_type_for_content_type_uses_fixed_allowlist(ct: str | None, expected: str) -> None:
    assert _media_type_for_content_type(ct) == expected


def test_media_type_is_always_under_image_so_the_converter_can_decode_it() -> None:
    """Every media type this helper can return must be under ``image/``.

    The converter strips a ``data:`` prefix matching ``^data:image/.+;base64,``.
    A media type outside ``image/`` leaves the prefix inside the payload, and the
    decode then fails silently.
    """
    candidates = ["image/png", "text/html", "application/octet-stream", "", None, "video/mp4"]
    assert all(_media_type_for_content_type(ct).startswith("image/") for ct in candidates)


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


def _patch_fetch_always(monkeypatch: pytest.MonkeyPatch, payload: bytes = b"\x89PNGdata") -> list[str]:
    """Stub `egress_fetch_bytes` to succeed for any URL; return the call log."""
    called: list[str] = []

    async def fake(url: str, **_kwargs: object) -> tuple[bytes, dict[str, str]]:
        called.append(url)
        return payload, {"content-type": "image/png"}

    monkeypatch.setattr(html_prefetch, "egress_fetch_bytes", fake)
    return called


def _srcs(rewritten: str) -> list[str | None]:
    """Return the `src` attribute of every `<img>` in document order."""
    return [img.get("src") for img in lxml.html.fromstring(rewritten).findall(".//img")]


def _non_admission_classes(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return the `non_admission_class` of every per-reference non-admission record."""
    return [rec.non_admission_class for rec in caplog.records if hasattr(rec, "non_admission_class")]


# --- Admitted references -----------------------------------------------------


@pytest.mark.asyncio
async def test_admitted_image_is_carried_inline_and_copied_into_the_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An admitted image becomes a `data:` URI, and its workspace copy is still written."""
    payload = b"\x89PNGsmall"
    _patch_fetch(monkeypatch, {"https://cdn.example/a.png": (payload, {"content-type": "image/png"})})

    html = '<html><body><img src="https://cdn.example/a.png"></body></html>'
    rewritten = await prefetch_images(html, tmp_path)

    expected_uri = "data:image/png;base64," + base64.b64encode(payload).decode()
    assert _srcs(rewritten) == [expected_uri]

    saved = tmp_path / "prefetched-images" / f"{hashlib.sha256(payload).hexdigest()}.png"
    assert saved.read_bytes() == payload


@pytest.mark.asyncio
async def test_unlisted_content_type_still_admits_with_a_generic_image_media_type(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A server returning text/html still yields a `.bin` file and a decodable `data:` URI."""
    payload = b"<html>fake</html>"
    _patch_fetch(monkeypatch, {"https://cdn.example/x.png": (payload, {"content-type": "text/html"})})

    html = '<html><body><img src="https://cdn.example/x.png"></body></html>'
    rewritten = await prefetch_images(html, tmp_path)

    src = _srcs(rewritten)[0]
    assert src is not None
    assert src.startswith("data:image/octet-stream;base64,")
    saved = tmp_path / "prefetched-images" / f"{hashlib.sha256(payload).hexdigest()}.bin"
    assert saved.read_bytes() == payload


# --- References that carry their own bytes -----------------------------------


@pytest.mark.asyncio
async def test_data_uri_passes_through_without_fetch_or_read(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A `data:` URI already carries its bytes, so it is admitted with no I/O."""
    called = _patch_fetch(monkeypatch, {})

    html = '<html><body><img src="data:image/png;base64,AAAA"></body></html>'
    rewritten = await prefetch_images(html, tmp_path)

    assert called == []
    assert _srcs(rewritten) == ["data:image/png;base64,AAAA"]


@pytest.mark.parametrize("scheme", ["DATA:", "Data:", "dAtA:"])
@pytest.mark.asyncio
async def test_mixed_case_data_uri_is_normalised_so_the_converter_still_decodes_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scheme: str
) -> None:
    """A browser-valid `DATA:` URI must survive admission in a form the converter accepts.

    The converter matches the `data:` scheme case-sensitively. Detecting the
    reference without normalising it would pass `DATA:` through untouched, and
    the image would then be lost inside the converter with no record — the
    silent-loss failure this admission boundary exists to remove.
    """
    called = _patch_fetch(monkeypatch, {})

    html = f'<html><body><img src="{scheme}image/png;base64,AAAA"></body></html>'
    rewritten = await prefetch_images(html, tmp_path, source_url="https://origin.example/a/page.html")

    assert called == [], "a reference carrying its own bytes must not be fetched"
    assert _srcs(rewritten) == ["data:image/png;base64,AAAA"]


# --- References that name no resource ----------------------------------------


@pytest.mark.parametrize("src", ["", "   ", "#", "#section-2"])
@pytest.mark.asyncio
async def test_reference_naming_no_resource_is_neither_fetched_nor_dropped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, src: str
) -> None:
    """Blank and fragment-only references name no resource, so they are left alone.

    Resolving a fragment-only reference would produce the source URL itself,
    turning `#x` into a fetch of the page whose HTML would then be inlined as
    image bytes.
    """
    called = _patch_fetch(monkeypatch, {})

    html = f'<html><body><img src="{src}"></body></html>'
    rewritten = await prefetch_images(html, tmp_path, source_url="https://origin.example/a/page.html")

    assert called == []
    # The attribute is left as it was found. Comparison is on the stripped value
    # because lxml normalises whitespace-only attribute values when parsing.
    assert (_srcs(rewritten)[0] or "").strip() == src.strip()


# --- Resolution against the source URL ---------------------------------------


@pytest.mark.asyncio
async def test_page_relative_reference_is_resolved_against_the_source_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A page-relative `src` is resolved so the egress policy can evaluate it as a URL."""
    called = _patch_fetch_always(monkeypatch)

    html = '<html><body><img src="images/photo.png"></body></html>'
    await prefetch_images(html, tmp_path, source_url="https://origin.example/a/b/page.html")

    assert called == ["https://origin.example/a/b/images/photo.png"]


@pytest.mark.asyncio
async def test_relative_references_key_to_the_source_host_not_a_shared_empty_bucket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Resolution must precede per-host keying, or one page's own images trip the host cap.

    Eleven page-relative references against a per-host cap of ten: keying them
    before resolution would bucket all eleven under the empty host and drop the
    eleventh, even though they are the source page's own images.
    """
    called = _patch_fetch_always(monkeypatch)

    imgs = "".join(f'<img src="images/{i}.png">' for i in range(11))
    html = f"<html><body>{imgs}</body></html>"
    rewritten = await prefetch_images(
        html,
        tmp_path,
        policy=PrefetchPolicy(max_images_per_host=10),
        source_url="https://origin.example/a/b/page.html",
    )

    # The per-host cap of 10 applies to origin.example, so the eleventh is
    # dropped — but on the source host's own budget, not a shared empty bucket.
    assert len(called) == 10
    assert all(url.startswith("https://origin.example/a/b/images/") for url in called)
    srcs = _srcs(rewritten)
    assert sum(1 for s in srcs if s is not None and s.startswith("data:")) == 10
    assert srcs[-1] is None


@pytest.mark.asyncio
async def test_absent_source_url_leaves_references_unresolved(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """With no source URL there is nothing to resolve against; absolute references still work."""
    called = _patch_fetch_always(monkeypatch)

    html = '<html><body><img src="https://cdn.example/a.png"></body></html>'
    rewritten = await prefetch_images(html, tmp_path, source_url=None)

    assert called == ["https://cdn.example/a.png"]
    assert _srcs(rewritten)[0].startswith("data:")


# --- Non-admission: every reference is dropped and recorded ------------------
#
# The admission loop has eight non-admission arms. Four are driven by
# per-document caps and four by a failed fetch. Each is an early exit that
# bypasses the admission path, so each needs its own evidence.


def _cap_case(kind: str) -> tuple[str, PrefetchPolicy, str]:
    """Return (html, policy, expected class) for a cap arm that drops the last <img>."""
    two = '<img src="https://cdn.example/1.png"><img src="https://cdn.example/2.png">'
    if kind == "max_images":
        return f"<html><body>{two}</body></html>", PrefetchPolicy(max_images=1), "max_images"
    if kind == "max_total_bytes":
        # The first image consumes the whole byte budget, so the second is dropped.
        return f"<html><body>{two}</body></html>", PrefetchPolicy(max_total_bytes=1), "max_total_bytes"
    if kind == "max_images_per_host":
        return (
            f"<html><body>{two}</body></html>",
            PrefetchPolicy(max_images_per_host=1),
            "max_images_per_host",
        )
    if kind == "phase_deadline_seconds":
        return (
            f"<html><body>{two}</body></html>",
            PrefetchPolicy(phase_deadline_seconds=-1.0),
            "phase_deadline_seconds",
        )
    raise AssertionError(f"unknown cap arm: {kind}")


@pytest.mark.parametrize(
    "kind",
    ["max_images", "max_total_bytes", "max_images_per_host", "phase_deadline_seconds"],
)
@pytest.mark.asyncio
async def test_cap_driven_non_admission_drops_src_and_records_the_class(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture, kind: str
) -> None:
    """Each per-document cap removes the `src` and records the reference with its class."""
    _patch_fetch_always(monkeypatch)
    html, policy, expected_class = _cap_case(kind)

    with caplog.at_level(logging.DEBUG, logger=html_prefetch.logger.name):
        rewritten = await prefetch_images(html, tmp_path, policy=policy)

    # The deadline arm drops both images; every other arm admits the first.
    srcs = _srcs(rewritten)
    assert srcs[-1] is None, "an un-admitted reference must not remain resolvable"
    assert expected_class in _non_admission_classes(caplog)


def _failure_fetch(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    """Stub `egress_fetch_bytes` to raise `exc` for every URL."""

    async def fake(url: str, **_kwargs: object) -> tuple[bytes, dict[str, str]]:
        raise exc

    monkeypatch.setattr(html_prefetch, "egress_fetch_bytes", fake)


@pytest.mark.parametrize(
    "exc, expected_class",
    [
        (DenyListDestination("denied"), "egress_policy"),
        (FetchTooLargeError("over cap"), "per_image_max_bytes"),
        # socket.gaierror and ConnectionError are OSError subclasses, so a name
        # resolution failure must not be reported as a disk problem.
        (socket.gaierror("name resolution failed"), "network_error"),
        (RuntimeError("boom"), "unexpected_error"),
    ],
)
@pytest.mark.asyncio
async def test_failed_fetch_drops_src_and_records_the_class(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    exc: Exception,
    expected_class: str,
) -> None:
    """Each fetch failure removes the `src` and records the reference with its class."""
    _failure_fetch(monkeypatch, exc)

    html = '<html><body><img src="https://cdn.example/a.png"></body></html>'
    with caplog.at_level(logging.DEBUG, logger=html_prefetch.logger.name):
        rewritten = await prefetch_images(html, tmp_path)

    assert _srcs(rewritten) == [None]
    assert _non_admission_classes(caplog) == [expected_class]


@pytest.mark.asyncio
async def test_failed_workspace_write_is_recorded_as_a_disk_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A genuine write failure is classed apart from the network errors that share its type."""
    _patch_fetch_always(monkeypatch)

    def _refuse_write(self, *_args: object, **_kwargs: object) -> int:
        raise OSError("no space left on device")

    monkeypatch.setattr(Path, "write_bytes", _refuse_write)

    html = '<html><body><img src="https://cdn.example/a.png"></body></html>'
    with caplog.at_level(logging.DEBUG, logger=html_prefetch.logger.name):
        rewritten = await prefetch_images(html, tmp_path)

    assert _srcs(rewritten) == [None]
    assert _non_admission_classes(caplog) == ["disk_error"]


@pytest.mark.parametrize("src", ["http://[::1/x.png", "https://[1:2:3:4:5:6:7:8:9]/a.png"])
@pytest.mark.parametrize("source_url", [None, "https://origin.example/a/page.html"])
@pytest.mark.asyncio
async def test_unparsable_reference_drops_one_image_without_failing_the_document(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    src: str,
    source_url: str | None,
) -> None:
    """A malformed authority must cost one image, not the whole conversion.

    `urljoin` and `urlparse` both raise `ValueError` on unbalanced IPv6 brackets
    and bad IPv6 literals. Letting that escape would turn a single hostile
    attribute into a failed job whose error message carries page-controlled text.
    """
    _patch_fetch_always(monkeypatch)

    html = f'<html><body><img src="{src}"><img src="https://cdn.example/ok.png"></body></html>'
    with caplog.at_level(logging.DEBUG, logger=html_prefetch.logger.name):
        rewritten = await prefetch_images(html, tmp_path, source_url=source_url)

    srcs = _srcs(rewritten)
    assert srcs[0] is None, "the unparsable reference must be dropped"
    assert srcs[1].startswith("data:"), "the rest of the document must still be admitted"
    assert "malformed_reference" in _non_admission_classes(caplog)


@pytest.mark.asyncio
async def test_deny_set_reference_is_dropped_while_the_phase_returns_normally(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An egress rejection while admitting a referenced resource must not fail the job."""
    payload = b"OK"

    async def fake(url: str, **_kwargs: object) -> tuple[bytes, dict[str, str]]:
        if "169.254.169.254" in url:
            raise DenyListDestination("denied")
        return payload, {"content-type": "image/png"}

    monkeypatch.setattr(html_prefetch, "egress_fetch_bytes", fake)

    html = (
        '<html><body><img src="http://169.254.169.254/secret.png"><img src="https://cdn.example/ok.png"></body></html>'
    )
    with caplog.at_level(logging.DEBUG, logger=html_prefetch.logger.name):
        rewritten = await prefetch_images(html, tmp_path)

    srcs = _srcs(rewritten)
    assert srcs[0] is None, "the deny-set reference must not remain resolvable"
    assert srcs[1].startswith("data:"), "the admissible image is unaffected"
    assert "egress_policy" in _non_admission_classes(caplog)


# --- Per-document caps: admitted counts --------------------------------------


@pytest.mark.asyncio
async def test_image_count_cap_admits_up_to_the_cap_and_drops_the_rest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With 51 <img> tags, 50 are admitted and the 51st is dropped."""
    called = _patch_fetch_always(monkeypatch)

    imgs = "".join(f'<img src="https://cdn.example/{i}.png">' for i in range(51))
    html = f"<html><body>{imgs}</body></html>"
    # Relax the per-host cap so this test isolates the document-level count cap.
    rewritten = await prefetch_images(html, tmp_path, policy=PrefetchPolicy(max_images_per_host=100))

    assert len(called) == 50, "the 51st reference is never fetched"
    srcs = _srcs(rewritten)
    assert sum(1 for s in srcs if s is not None and s.startswith("data:")) == 50
    assert sum(1 for s in srcs if s is None) == 1


@pytest.mark.asyncio
async def test_per_host_cap_drops_images_beyond_the_cap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Many <img> tags at one host are capped, bounding outbound amplification."""
    called = _patch_fetch_always(monkeypatch)

    imgs = "".join(f'<img src="https://victim.example/{i}.png">' for i in range(15))
    html = f"<html><body>{imgs}</body></html>"
    rewritten = await prefetch_images(html, tmp_path, policy=PrefetchPolicy(max_images_per_host=5))

    assert len(called) == 5
    srcs = _srcs(rewritten)
    assert sum(1 for s in srcs if s is not None and s.startswith("data:")) == 5
    assert sum(1 for s in srcs if s is None) == 10


@pytest.mark.asyncio
async def test_per_host_cap_counts_each_host_separately(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The per-host cap buckets by hostname, so two hosts get independent budgets."""
    called = _patch_fetch_always(monkeypatch)

    imgs = "".join(f'<img src="https://a.example/{i}.png">' for i in range(3)) + "".join(
        f'<img src="https://b.example/{i}.png">' for i in range(3)
    )
    html = f"<html><body>{imgs}</body></html>"
    rewritten = await prefetch_images(html, tmp_path, policy=PrefetchPolicy(max_images_per_host=3))

    assert len(called) == 6
    assert all(s is not None and s.startswith("data:") for s in _srcs(rewritten))


# --- Phase summary -----------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_accounts_for_cap_dropped_references(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Cap-driven drops must appear in the summary, not be reported as zero skipped.

    Without this, a page whose images were all dropped by a cap is indistinguishable
    from a page that had no images.
    """
    _patch_fetch_always(monkeypatch)

    imgs = "".join(f'<img src="https://cdn.example/{i}.png">' for i in range(5))
    html = f"<html><body>{imgs}</body></html>"
    with caplog.at_level(logging.INFO, logger=html_prefetch.logger.name):
        await prefetch_images(html, tmp_path, policy=PrefetchPolicy(max_images=2))

    summary = next(rec.getMessage() for rec in caplog.records if "admitted" in rec.getMessage())
    assert "admitted 2" in summary
    assert "not admitted 3" in summary
    assert "cap_dropped=3" in summary
