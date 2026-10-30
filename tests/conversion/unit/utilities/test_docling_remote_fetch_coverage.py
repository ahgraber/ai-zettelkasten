"""Spike tests and regression tests for Docling HTML backend remote-fetch coverage
and workspace-confinement.

Spike A — remote-fetch coverage:
    With ``enable_remote_fetch=False``, Docling's HTML backend makes zero
    outbound network calls regardless of which resource-fetching tags appear in
    the HTML.  Only ``<img src>`` reaches Docling's ``ImageResourceLoader``,
    whose remote fetches open connections through the module-level
    ``_open_direct`` / ``_open_with_proxy`` helpers; every other tag type
    (``<link>``, ``<script>``, ``<iframe>``, ``srcset``, ``<picture><source>``,
    CSS ``url()``, SVG ``<image>``) produces no I/O.

Spike B — workspace containment:
    Docling has no option to confine local image reads to a caller-chosen
    directory, and it joins absolute local paths onto a remote ``source_uri``.
    Containment and loading of prefetched workspace images are provided by our
    ``ConfinedHTMLDocumentBackend`` subclass returned by ``make_confined_backend``.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from pydantic import AnyUrl
import pytest

import docling.backend.utils.image_resource_loader as _image_loader_mod
from docling.datamodel.backend_options import HTMLBackendOptions
from docling.datamodel.base_models import InputFormat
from docling.datamodel.document import InputDocument

from aizk.conversion.core.errors import WorkspaceEscape
from aizk.conversion.utilities.docling_backend import make_confined_backend

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_input_doc(
    html: str,
    backend_class,
    options: HTMLBackendOptions,
) -> InputDocument:
    """Build a minimal InputDocument from an HTML string."""
    return InputDocument(
        path_or_stream=BytesIO(html.encode()),
        format=InputFormat.HTML,
        filename="test.html",
        backend=backend_class,
        backend_options=options,
    )


def _record_outbound_connections(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace Docling's connection openers with recorders that refuse to connect.

    Covers both the direct path and the environment-proxy path, so the test does
    not depend on proxy variables.  The recorder raises ``ValueError``, which
    Docling's image loader turns into a warning.
    """
    opened_urls: list[str] = []

    def _record_and_refuse(url: str, headers: dict[str, str]):
        opened_urls.append(url)
        raise ValueError("test: blocking outbound connection")

    monkeypatch.setattr(_image_loader_mod, "_open_direct", _record_and_refuse)
    monkeypatch.setattr(_image_loader_mod, "_open_with_proxy", _record_and_refuse)
    return opened_urls


_ALL_RESOURCE_HTML = """
<html>
<head>
  <link rel="stylesheet" href="https://cdn.example/style.css">
  <script src="https://cdn.example/script.js"></script>
</head>
<body>
  <img src="https://cdn.example/img-src.png">
  <img srcset="https://cdn.example/img-srcset.png 1x">
  <picture>
    <source srcset="https://cdn.example/picture-source.webp">
  </picture>
  <iframe src="https://cdn.example/frame.html"></iframe>
  <style>div { background: url(https://cdn.example/css-bg.png); }</style>
  <svg><image href="https://cdn.example/svg-image.png"/></svg>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Spike A: enable_remote_fetch=False blocks all network I/O
# ---------------------------------------------------------------------------


def test_spike_a_no_outbound_connection_with_remote_fetch_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spike A: with enable_remote_fetch=False, Docling opens no outbound connection.

    Docling's HTML backend fetches remote resources only for <img src>, through
    ImageResourceLoader, which refuses before connecting when remote fetch is
    disabled.  All other resource types are ignored.
    """
    from docling.backend.html_backend import HTMLDocumentBackend

    opened_urls = _record_outbound_connections(monkeypatch)

    options = HTMLBackendOptions(
        kind="html",
        fetch_images=True,
        enable_remote_fetch=False,
        enable_local_fetch=False,
    )
    in_doc = _make_input_doc(_ALL_RESOURCE_HTML, HTMLDocumentBackend, options)
    assert in_doc._backend is not None
    in_doc._backend.convert()

    assert opened_urls == [], "Docling opened a connection despite enable_remote_fetch=False"


def test_spike_a_remote_fetch_true_opens_outbound_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spike A (inverse): with enable_remote_fetch=True, Docling opens a connection.

    Confirms that the test above is meaningful — the recorded openers are
    Docling's real fetch path and are reached when remote fetch is enabled.
    """
    from docling.backend.html_backend import HTMLDocumentBackend

    opened_urls = _record_outbound_connections(monkeypatch)

    options = HTMLBackendOptions(
        kind="html",
        fetch_images=True,
        enable_remote_fetch=True,
        enable_local_fetch=False,
    )
    in_doc = _make_input_doc(
        '<html><body><img src="https://cdn.example/img.png"></body></html>',
        HTMLDocumentBackend,
        options,
    )
    assert in_doc._backend is not None
    in_doc._backend.convert()

    assert opened_urls == ["https://cdn.example/img.png"]


# ---------------------------------------------------------------------------
# Spike B / ConfinedHTMLDocumentBackend — workspace containment
# ---------------------------------------------------------------------------


def test_confined_backend_blocks_path_traversal(tmp_path: Path) -> None:
    """Spike B: a path that resolves outside the workspace raises WorkspaceEscape.

    Verifies the subclassing approach is required — Docling has no option that
    confines image reads to the job workspace.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    # File physically outside the workspace.
    outside = tmp_path / "secret.txt"
    outside.write_bytes(b"sensitive data")

    # Crafted src traverses out of the workspace.
    traversal_src = str(workspace / ".." / "secret.txt")

    confined_cls = make_confined_backend(workspace)
    options = HTMLBackendOptions(
        kind="html",
        fetch_images=True,
        enable_remote_fetch=False,
        enable_local_fetch=True,
    )
    html = f'<html><body><img src="{traversal_src}"></body></html>'
    in_doc = _make_input_doc(html, confined_cls, options)
    assert in_doc._backend is not None

    with pytest.raises(WorkspaceEscape):
        in_doc._backend.convert()


def _make_png_bytes() -> bytes:
    """Return bytes for a valid 1×1 red PNG image."""
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (1, 1), color=(255, 0, 0)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.mark.parametrize(
    "source_uri",
    [None, "https://origin.example/post"],
    ids=["no-source-uri", "remote-source-uri"],
)
def test_confined_backend_loads_prefetched_workspace_image(tmp_path: Path, source_uri: str | None) -> None:
    """A prefetched workspace image referenced by absolute path is embedded in the document.

    The remote ``source_uri`` case is the converter's production shape: Docling
    would join the absolute path onto the origin URL and refuse it as a remote
    fetch, dropping the image.
    """
    workspace = tmp_path / "workspace"
    (workspace / "prefetched-images").mkdir(parents=True)

    img_path = workspace / "prefetched-images" / "image.png"
    img_path.write_bytes(_make_png_bytes())

    confined_cls = make_confined_backend(workspace)
    options = HTMLBackendOptions(
        kind="html",
        fetch_images=True,
        enable_remote_fetch=False,
        enable_local_fetch=True,
        source_uri=AnyUrl(source_uri) if source_uri else None,
    )
    html = f'<html><body><img src="{img_path}"></body></html>'
    in_doc = _make_input_doc(html, confined_cls, options)
    assert in_doc._backend is not None

    doc = in_doc._backend.convert()

    assert len(doc.pictures) == 1
    image = doc.pictures[0].image
    assert image is not None, "prefetched workspace image was not loaded into the document"
    assert (image.size.width, image.size.height) == (1, 1)


def test_confined_backend_keeps_root_relative_links_on_source_origin(tmp_path: Path) -> None:
    """Root-relative hrefs still resolve against a remote ``source_uri``.

    Only absolute paths inside the workspace are kept local; ``/about`` is not
    inside the workspace and must resolve to the origin site.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    confined_cls = make_confined_backend(workspace)
    options = HTMLBackendOptions(
        kind="html",
        fetch_images=True,
        enable_remote_fetch=False,
        enable_local_fetch=True,
        source_uri=AnyUrl("https://origin.example/post"),
    )
    html = '<html><body><p><a href="/about">About</a></p></body></html>'
    in_doc = _make_input_doc(html, confined_cls, options)
    assert in_doc._backend is not None

    doc = in_doc._backend.convert()

    hyperlinks = {str(item.hyperlink) for item in doc.texts if item.hyperlink}
    assert hyperlinks == {"https://origin.example/about"}


def test_confined_backend_blocks_absolute_path_outside_workspace(
    tmp_path: Path,
) -> None:
    """An absolute path pointing outside the workspace is rejected."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    outside = tmp_path / "other_file.txt"
    outside.write_bytes(b"outside data")

    confined_cls = make_confined_backend(workspace)
    options = HTMLBackendOptions(
        kind="html",
        fetch_images=True,
        enable_remote_fetch=False,
        enable_local_fetch=True,
    )
    html = f'<html><body><img src="{outside}"></body></html>'
    in_doc = _make_input_doc(html, confined_cls, options)
    assert in_doc._backend is not None

    with pytest.raises(WorkspaceEscape):
        in_doc._backend.convert()


def test_confined_backend_blocks_remote_url_without_outbound_connection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """ConfinedHTMLDocumentBackend drops remote URLs before any connection is opened.

    Remote fetch is enabled in the options so the assertion exercises the
    backend's own block rather than Docling's ``enable_remote_fetch`` gate.
    """
    opened_urls = _record_outbound_connections(monkeypatch)

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    confined_cls = make_confined_backend(workspace)
    options = HTMLBackendOptions(
        kind="html",
        fetch_images=True,
        enable_remote_fetch=True,
        enable_local_fetch=True,
    )
    html = '<html><body><img src="https://cdn.example/remote.png"></body></html>'
    in_doc = _make_input_doc(html, confined_cls, options)
    assert in_doc._backend is not None
    in_doc._backend.convert()

    assert opened_urls == []
