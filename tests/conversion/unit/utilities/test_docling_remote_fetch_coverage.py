"""Tests for the converter's dereference surface and the admission boundary end to end.

The pipeline's guarantee is that the converter dereferences no location it finds
in the document it is given. Admission covers ``<img src>``, which it must rewrite
anyway; every other resource-bearing shape is covered by the converter being
configured so it cannot fetch at all.

The multi-shape coverage test here is the standing evidence for that second half:
it feeds a document naming resources in every shape a page might plausibly use and
asserts none of them produces an outbound request. It is the check that would catch
the converter growing a new dereference site on a version bump.
"""

from __future__ import annotations

import asyncio
import base64
from io import BytesIO
from pathlib import Path

import pytest

from docling.backend.html_backend import HTMLDocumentBackend
import docling.backend.utils.image_resource_loader as _image_loader_mod
from docling.datamodel.backend_options import HTMLBackendOptions
from docling.datamodel.base_models import InputFormat
from docling.datamodel.document import InputDocument
from docling_core.types.doc.document import PictureItem

from aizk.conversion.utilities import html_prefetch
from aizk.conversion.utilities.html_prefetch import prefetch_images

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_input_doc(html: str, options: HTMLBackendOptions) -> InputDocument:
    """Build a minimal InputDocument from an HTML string."""
    return InputDocument(
        path_or_stream=BytesIO(html.encode()),
        format=InputFormat.HTML,
        filename="document.html",
        backend=HTMLDocumentBackend,
        backend_options=options,
    )


def _pipeline_backend_options(**overrides: object) -> HTMLBackendOptions:
    """Return backend options matching the pipeline's converter configuration."""
    defaults: dict[str, object] = {
        "kind": "html",
        "fetch_images": True,
        "enable_remote_fetch": False,
        "enable_local_fetch": False,
        "render_page": False,
    }
    defaults.update(overrides)
    return HTMLBackendOptions(**defaults)  # type: ignore[arg-type]


def _pictures(doc) -> list[PictureItem]:
    """Return every PictureItem in the converted document."""
    return [item for item, _ in doc.iterate_items() if isinstance(item, PictureItem)]


def _png_bytes() -> bytes:
    """Return bytes for a valid 1x1 red PNG image."""
    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (1, 1), color=(255, 0, 0)).save(buf, format="PNG")
    return buf.getvalue()


# Every resource-bearing shape a page might use to name a location, including
# the ones the admission step does not recognise. None of these may produce I/O.
_ALL_RESOURCE_HTML = """
<html>
<head>
  <link rel="stylesheet" href="https://cdn.example/style.css">
  <link rel="preload" as="image" imagesrcset="https://cdn.example/preload.png 1x">
  <script src="https://cdn.example/script.js"></script>
</head>
<body>
  <img src="https://cdn.example/img-src.png">
  <img srcset="https://cdn.example/img-srcset.png 1x">
  <picture>
    <source srcset="https://cdn.example/picture-source.webp">
  </picture>
  <iframe src="https://cdn.example/frame.html"></iframe>
  <object data="https://cdn.example/object-data.png"></object>
  <embed src="https://cdn.example/embed-src.png">
  <video poster="https://cdn.example/poster.png"></video>
  <style>div { background: url(https://cdn.example/css-bg.png); }</style>
  <div style="background: url(https://cdn.example/inline-bg.png)"></div>
  <svg><image href="https://cdn.example/svg-image.png"/></svg>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# The converter's dereference surface
# ---------------------------------------------------------------------------


def test_no_resource_shape_produces_a_dereference(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Under the pipeline's configuration, no reference shape reaches the network or the disk.

    This is the standing evidence that stripping `<img src>` alone is sufficient:
    everything else is unresolvable because the converter cannot dereference,
    not because we removed it from the document. It must keep passing on every
    version bump — a new dereference site in the converter shows up here first.

    Both halves are checked. Watching only outbound HTTP would miss a shape that
    the converter resolves to a local path instead.
    """
    target = tmp_path / "local-resource.png"
    target.write_bytes(b"\x89PNG local")

    def _fail_on_get(self, url, **kwargs):
        raise AssertionError(f"unexpected outbound request: {url}")

    monkeypatch.setattr(_image_loader_mod.requests.Session, "get", _fail_on_get)

    opened: list[str] = []
    real_open = Path.open

    def _record_open(self, *args, **kwargs):
        opened.append(str(self))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _record_open)

    # Name the same set of shapes at a real local file as well as at remote URLs,
    # so a converter that resolved any of them locally would be caught.
    local_shapes = (
        f'<img src="{target}">'
        f'<img srcset="{target} 1x">'
        f'<picture><source srcset="{target}"></picture>'
        f'<object data="{target}"></object>'
        f'<embed src="{target}">'
        f'<video poster="{target}"></video>'
        f'<div style="background: url({target})"></div>'
        f'<svg><image href="{target}"/></svg>'
    )
    html = _ALL_RESOURCE_HTML.replace("</body>", f"{local_shapes}</body>")

    in_doc = _make_input_doc(html, _pipeline_backend_options())
    assert in_doc._backend is not None
    in_doc._backend.convert()

    # Scoped to paths the page named. The converter legitimately opens its own
    # bundled resources (charset-detection models and the like) during a parse.
    page_named = [path for path in opened if str(tmp_path) in path]
    assert page_named == [], f"conversion dereferenced a page-named local path: {page_named}"


def test_remote_fetch_when_enabled_reaches_the_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inverse control: the mock above is genuinely on the path the converter would use.

    Without this, a passing coverage test could mean the patch target was wrong
    rather than that no request was made.
    """
    requested: list[str] = []

    def _record_get(self, url, **kwargs):
        requested.append(url)
        import requests as _requests

        raise _requests.HTTPError("blocked for test")

    monkeypatch.setattr(_image_loader_mod.requests.Session, "get", _record_get)

    options = _pipeline_backend_options(enable_remote_fetch=True)
    in_doc = _make_input_doc('<html><body><img src="https://cdn.example/img.png"></body></html>', options)
    assert in_doc._backend is not None
    in_doc._backend.convert()

    assert requested == ["https://cdn.example/img.png"]


def test_local_reference_is_not_read_even_when_the_file_exists(tmp_path: Path) -> None:
    """A local path surviving into the document is refused by the converter itself.

    Admission strips such references, so this covers the backstop rather than the
    gate: even handed a path to a real file, the converter opens nothing.
    """
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"sensitive data")

    html = f'<html><body><img src="{secret}"></body></html>'
    in_doc = _make_input_doc(html, _pipeline_backend_options())
    assert in_doc._backend is not None
    doc = in_doc._backend.convert()

    pictures = _pictures(doc)
    assert len(pictures) == 1
    assert pictures[0].image is None, "no local file may be read into the document"


# ---------------------------------------------------------------------------
# Admission end to end
# ---------------------------------------------------------------------------


def _deny_egress(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the egress gate to refuse every destination.

    A host-named reference resolves to an ordinary outbound URL, so without this
    the admission step would attempt a real DNS lookup and the outcome would
    depend on the resolver rather than on the policy under test.
    """
    from aizk.conversion.core.errors import DenyListDestination

    async def _refuse(url: str, **_kwargs: object) -> tuple[bytes, dict[str, str]]:
        raise DenyListDestination(f"denied: {url}")

    monkeypatch.setattr(html_prefetch, "egress_fetch_bytes", _refuse)


def _convert_after_admission(
    html: str,
    workspace: Path,
    *,
    source_url: str | None = None,
):
    """Run the admission step, then convert the result as the pipeline does."""
    rewritten = asyncio.run(prefetch_images(html, workspace, source_url=source_url))
    in_doc = _make_input_doc(rewritten, _pipeline_backend_options())
    assert in_doc._backend is not None
    return rewritten, in_doc._backend.convert()


def _recorded_classes(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return the non-admission class of every per-reference record."""
    return [rec.non_admission_class for rec in caplog.records if hasattr(rec, "non_admission_class")]


def test_hostile_local_reference_is_stripped_recorded_and_never_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A page naming a host file loses the reference at admission and it is never read."""
    import logging

    _deny_egress(monkeypatch)

    html = '<html><body><p>text</p><img src="/etc/ssh/ssh_host_rsa_key"></body></html>'
    with caplog.at_level(logging.DEBUG, logger=html_prefetch.logger.name):
        rewritten, doc = _convert_after_admission(html, tmp_path, source_url="https://origin.example/article")

    assert "ssh_host_rsa_key" not in rewritten, "the reference must not survive admission"
    pictures = _pictures(doc)
    assert len(pictures) == 1
    assert pictures[0].image is None
    # The specific class matters: a generic "something was recorded" assertion
    # would also pass if the reference had merely failed to resolve.
    assert _recorded_classes(caplog) == ["egress_policy"]


def test_traversal_reference_is_stripped_and_never_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A traversal reference is likewise dropped before the converter sees it."""
    import logging

    _deny_egress(monkeypatch)

    html = '<html><body><img src="../../../../etc/passwd"></body></html>'
    with caplog.at_level(logging.DEBUG, logger=html_prefetch.logger.name):
        rewritten, doc = _convert_after_admission(html, tmp_path, source_url="https://origin.example/a/article")

    assert "etc/passwd" not in rewritten
    assert _pictures(doc)[0].image is None
    assert _recorded_classes(caplog) == ["egress_policy"]


def test_admitted_image_reaches_the_document_without_a_local_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An admitted image is decoded into the document, and no file is opened to do it."""
    payload = _png_bytes()

    async def fake_fetch(url: str, **_kwargs: object) -> tuple[bytes, dict[str, str]]:
        return payload, {"content-type": "image/png"}

    monkeypatch.setattr(html_prefetch, "egress_fetch_bytes", fake_fetch)

    opened: list[str] = []
    real_open = Path.open

    def _record_open(self, *args, **kwargs):
        opened.append(str(self))
        return real_open(self, *args, **kwargs)

    html = '<html><body><img src="https://cdn.example/a.png"></body></html>'
    rewritten = asyncio.run(prefetch_images(html, tmp_path, source_url="https://origin.example/article"))

    # Patch only around conversion, so the admission step's own workspace write
    # is not counted as a converter read.
    monkeypatch.setattr(Path, "open", _record_open)
    in_doc = _make_input_doc(rewritten, _pipeline_backend_options())
    assert in_doc._backend is not None
    doc = in_doc._backend.convert()

    pictures = _pictures(doc)
    assert len(pictures) == 1
    assert pictures[0].image is not None, "the admitted image must reach the document"
    assert opened == [], "conversion must open no file"


def test_page_authored_data_uri_is_admitted_without_fetch_or_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `data:` image the page authored needs neither a fetch nor a read."""

    async def _fail_fetch(url: str, **_kwargs: object) -> tuple[bytes, dict[str, str]]:
        raise AssertionError(f"unexpected fetch: {url}")

    monkeypatch.setattr(html_prefetch, "egress_fetch_bytes", _fail_fetch)

    encoded = base64.b64encode(_png_bytes()).decode()
    html = f'<html><body><img src="data:image/png;base64,{encoded}"></body></html>'
    _, doc = _convert_after_admission(html, tmp_path, source_url="https://origin.example/article")

    pictures = _pictures(doc)
    assert len(pictures) == 1
    assert pictures[0].image is not None


def test_document_whose_images_were_all_dropped_still_converts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Losing every image must degrade to placeholders, not fail the conversion."""
    from aizk.conversion.core.errors import DenyListDestination

    async def _deny(url: str, **_kwargs: object) -> tuple[bytes, dict[str, str]]:
        raise DenyListDestination("denied")

    monkeypatch.setattr(html_prefetch, "egress_fetch_bytes", _deny)

    html = '<html><body><p>body text</p><img src="https://cdn.example/a.png"></body></html>'
    _, doc = _convert_after_admission(html, tmp_path, source_url="https://origin.example/article")

    pictures = _pictures(doc)
    assert len(pictures) == 1
    assert pictures[0].image is None
    assert doc.export_to_markdown().strip(), "the document's text must survive"
