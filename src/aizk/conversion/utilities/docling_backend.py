"""Workspace-confined HTML document backend for Docling.

Docling's HTML backend handles ``<img src>`` in two steps:

1. ``HTMLDocumentBackend._resolve_relative_path`` resolves the ``src`` against
   ``HTMLBackendOptions.source_uri``.  A remote ``source_uri`` is joined with
   ``urljoin``, so an absolute local path such as
   ``/tmp/job/prefetched-images/x.png`` becomes
   ``https://origin.example/tmp/job/prefetched-images/x.png``.
2. ``HTMLDocumentBackend._create_image_ref`` hands the resolved location to
   ``ImageResourceLoader.create_image_ref``.  The loader fetches remote URLs only
   when ``enable_remote_fetch`` is set, and reads a local file only when
   ``enable_local_fetch`` is set *and* a local ``source_uri`` is present; it
   confines relative paths to the directory of that ``source_uri``.

Neither step knows about the job workspace.  With the converter's settings
(remote ``source_uri``, ``enable_remote_fetch=False``) every image that
:func:`html_prefetch.prefetch_images` copied into the workspace would be joined
onto the origin URL and refused as a remote fetch.

:func:`make_confined_backend` returns a ``HTMLDocumentBackend`` subclass that
closes over a workspace root and enforces:

* Remote image URLs are never fetched (images must already be pre-fetched into
  the workspace by :func:`html_prefetch.prefetch_images`).
* Local image paths are resolved and checked against the workspace via
  :meth:`pathlib.Path.is_relative_to`.  Paths that escape raise
  :class:`WorkspaceEscape`; paths inside the workspace are read through Docling's
  ``ImageResourceLoader`` so its size and pixel limits still apply.

Other resource-referencing constructs (``<link>``, ``<script>``, ``<iframe>``,
``srcset``, ``<picture><source>``, CSS ``url()``, SVG ``<image>``) produce no
I/O in Docling's HTML backend.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from docling.backend.html_backend import HTMLDocumentBackend
from docling.backend.utils.image_resource_loader import ImageResourceLoader
from docling_core.types.doc import ImageRef

from aizk.conversion.core.errors import WorkspaceEscape

logger = logging.getLogger(__name__)

_FILE_SCHEME_PREFIX = "file://"

# ImageResourceLoader reads a local file only when given a local base path; it
# confines to that path's parent directory.  A name inside the workspace makes
# the workspace that directory.
_WORKSPACE_BASE_NAME = "document.html"


def _strip_file_scheme(loc: str) -> str:
    loc = loc.strip()
    return loc[len(_FILE_SCHEME_PREFIX) :] if loc.startswith(_FILE_SCHEME_PREFIX) else loc


def make_confined_backend(workspace: Path) -> type[HTMLDocumentBackend]:
    """Return a ``HTMLDocumentBackend`` subclass confined to ``workspace``.

    The returned class closes over the resolved workspace root.  It overrides
    ``_resolve_relative_path`` and ``_create_image_ref`` to enforce two
    invariants:

    1. Remote image URLs are always rejected — no outbound requests are issued.
    2. Local image paths are resolved and compared against the workspace root;
       any path that falls outside raises :class:`WorkspaceEscape`, and any path
       inside is loaded into the document.

    The workspace root is resolved once at factory-call time so that symlink
    races between construction and the containment check cannot widen the
    allowed set.

    Args:
        workspace: The directory to which all local image paths are confined.
            Must be an existing directory when images are loaded, but need not
            exist at factory-call time.

    Returns:
        A fresh subclass of ``HTMLDocumentBackend`` with containment wired in.
        Each call returns a distinct class object suitable for use as
        ``HTMLFormatOption(backend=make_confined_backend(workspace), ...)``.
    """
    _workspace_resolved: Path = workspace.resolve()

    class _ConfinedHTMLDocumentBackend(HTMLDocumentBackend):
        """HTMLDocumentBackend with remote-fetch lockdown and workspace containment.

        Docling exposes no option for confining image reads to a caller-chosen
        directory, so this subclass overrides two private ``HTMLDocumentBackend``
        methods, ``_resolve_relative_path`` and ``_create_image_ref``.  They are
        not part of Docling's public API: a Docling upgrade can rename them or
        route image loading elsewhere, which would silently bypass both
        overrides.  ``tests/conversion/unit/utilities/test_docling_remote_fetch_coverage.py``
        asserts that a workspace image is loaded and that escapes raise, so such
        a change fails the suite.
        """

        def _resolve_relative_path(self, loc: str) -> str:
            """Keep absolute workspace paths local instead of joining them onto ``source_uri``.

            ``html_prefetch.prefetch_images`` rewrites each ``<img src>`` to an
            absolute path inside the workspace.  Docling would ``urljoin`` that
            path onto a remote ``source_uri``; returning it unchanged lets
            :meth:`_create_image_ref` read the local copy.  Every other location
            (including root-relative links such as ``/about``) is resolved by
            Docling.
            """
            path_str = _strip_file_scheme(loc)
            if ImageResourceLoader.is_local_path(path_str) and ImageResourceLoader.is_absolute_path(path_str):
                resolved = Path(path_str).resolve()
                if resolved.is_relative_to(_workspace_resolved):
                    return str(resolved)
            return super()._resolve_relative_path(loc)

        def _create_image_ref(self, src_url: str) -> Optional[ImageRef]:
            """Load an image, blocking remote fetches and enforcing workspace containment.

            Remote URLs are dropped (logged at WARNING).  ``data:`` URIs carry
            inline content and are delegated to Docling without a path check.
            Every other location is treated as a local path, resolved, and
            validated against the workspace root before it is read.

            Args:
                src_url: Image location as returned by :meth:`_resolve_relative_path`.

            Returns:
                The image on success, ``None`` when the image is skipped (remote
                URL, or a workspace file Docling cannot read or decode).

            Raises:
                WorkspaceEscape: When the resolved local path falls outside the
                    workspace root.
            """
            # Block remote fetches — images must be pre-fetched into the
            # workspace by html_prefetch.prefetch_images before Docling runs.
            if ImageResourceLoader.is_remote_url(src_url):
                logger.warning(
                    "Blocked remote image fetch in ConfinedHTMLDocumentBackend",
                    extra={"src_loc": src_url, "workspace": str(_workspace_resolved)},
                )
                return None

            # data: URIs carry inline content — delegate directly, no path check.
            if src_url.startswith("data:"):
                return super()._create_image_ref(src_url)

            resolved = Path(_strip_file_scheme(src_url)).resolve()
            if not resolved.is_relative_to(_workspace_resolved):
                logger.warning(
                    "Workspace escape rejected: local image path outside workspace",
                    extra={"src_loc": src_url, "resolved": str(resolved), "workspace": str(_workspace_resolved)},
                )
                raise WorkspaceEscape(
                    f"Image path resolves outside workspace: {resolved!s} is not within {_workspace_resolved!s}"
                )

            # Read through Docling's loader so its local-fetch switch and size
            # and pixel limits still apply.
            return self._image_loader.create_image_ref(str(resolved), str(_workspace_resolved / _WORKSPACE_BASE_NAME))

    _ConfinedHTMLDocumentBackend.__name__ = "ConfinedHTMLDocumentBackend"
    _ConfinedHTMLDocumentBackend.__qualname__ = "ConfinedHTMLDocumentBackend"
    return _ConfinedHTMLDocumentBackend


__all__ = ["make_confined_backend"]
