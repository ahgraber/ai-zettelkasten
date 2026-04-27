"""Spike tests and regression tests for Docling HTML backend remote-fetch coverage
and workspace-confinement.

Spike A — remote-fetch coverage:
    With ``enable_remote_fetch=False``, Docling's HTML backend makes zero
    outbound network calls regardless of which resource-fetching tags appear in
    the HTML.  Only ``<img src>`` ever triggers ``_load_image_data``; every
    other tag type (``<link>``, ``<script>``, ``<iframe>``, ``srcset``,
    ``<picture><source>``, CSS ``url()``, SVG ``<image>``) produces no I/O.

Spike B — local_fetch_root containment:
    ``HTMLBackendOptions`` has no ``local_fetch_root`` field.  The parent
    implementation opens local files with a bare ``open(src_loc, "rb")`` and
    no containment check.  Path traversal is only blocked by our
    ``ConfinedHTMLDocumentBackend`` subclass returned by ``make_confined_backend``.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest

import docling.backend.html_backend as _html_backend_mod
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


def test_spike_a_no_requests_get_calls_with_remote_fetch_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spike A: with enable_remote_fetch=False, requests.get is never called.

    Docling's HTML backend gates remote fetches only inside _load_image_data,
    which is only reached for <img src>.  All other resource types are ignored.
    Setting enable_remote_fetch=False therefore blocks all outbound I/O.
    """
    from docling.backend.html_backend import HTMLDocumentBackend

    requests_get_calls: list[tuple] = []

    def _fail_on_get(*args, **kwargs):
        requests_get_calls.append(args)
        raise AssertionError(f"Unexpected requests.get call: {args}")

    monkeypatch.setattr(_html_backend_mod.requests, "get", _fail_on_get)

    options = HTMLBackendOptions(
        kind="html",
        fetch_images=True,
        enable_remote_fetch=False,
        enable_local_fetch=False,
    )
    in_doc = _make_input_doc(_ALL_RESOURCE_HTML, HTMLDocumentBackend, options)
    assert in_doc._backend is not None
    in_doc._backend.convert()

    assert requests_get_calls == [], "requests.get was called despite enable_remote_fetch=False"


def test_spike_a_remote_fetch_true_would_call_requests_get(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spike A (inverse): with enable_remote_fetch=True, requests.get IS called.

    Confirms that the test above is meaningful — the mock is actually reached
    when remote fetch is enabled.
    """
    from docling.backend.html_backend import HTMLDocumentBackend

    requests_get_calls: list[str] = []

    def _record_get(url, **kwargs):
        requests_get_calls.append(url)
        # Raise HTTPError — Docling catches this and warns instead of propagating.
        import requests as _requests

        raise _requests.HTTPError("test: blocking outbound for spike")

    monkeypatch.setattr(_html_backend_mod.requests, "get", _record_get)

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

    assert len(requests_get_calls) == 1
    assert requests_get_calls[0] == "https://cdn.example/img.png"


# ---------------------------------------------------------------------------
# Spike B / ConfinedHTMLDocumentBackend — workspace containment
# ---------------------------------------------------------------------------


def test_confined_backend_blocks_path_traversal(tmp_path: Path) -> None:
    """Spike B: a path that resolves outside the workspace raises WorkspaceEscape.

    Verifies the subclassing approach is required — Docling's default backend
    would read the file because there is no ``local_fetch_root`` field.
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


def test_confined_backend_allows_workspace_local_image(tmp_path: Path) -> None:
    """A workspace-local image is read successfully by the confined backend."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    img_path = workspace / "image.png"
    img_path.write_bytes(_make_png_bytes())

    confined_cls = make_confined_backend(workspace)
    options = HTMLBackendOptions(
        kind="html",
        fetch_images=True,
        enable_remote_fetch=False,
        enable_local_fetch=True,
    )
    html = f'<html><body><img src="{img_path}"></body></html>'
    in_doc = _make_input_doc(html, confined_cls, options)
    assert in_doc._backend is not None
    # Conversion must complete without raising.
    in_doc._backend.convert()


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


def test_confined_backend_blocks_remote_url_without_requests(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """ConfinedHTMLDocumentBackend drops remote URLs before requests.get is called."""
    from docling.backend.html_backend import HTMLDocumentBackend

    requests_get_calls: list[str] = []

    def _fail_on_get(url, **kwargs):
        requests_get_calls.append(url)
        raise AssertionError(f"Unexpected requests.get: {url}")

    monkeypatch.setattr(_html_backend_mod.requests, "get", _fail_on_get)

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    confined_cls = make_confined_backend(workspace)
    options = HTMLBackendOptions(
        kind="html",
        fetch_images=True,
        enable_remote_fetch=False,
        enable_local_fetch=True,
    )
    html = '<html><body><img src="https://cdn.example/remote.png"></body></html>'
    in_doc = _make_input_doc(html, confined_cls, options)
    assert in_doc._backend is not None
    in_doc._backend.convert()

    assert requests_get_calls == []
