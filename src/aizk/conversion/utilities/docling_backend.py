"""Workspace-confined HTML document backend for Docling.

Docling's HTML backend only calls ``_load_image_data`` for ``<img src>``
attributes.  Other resource-referencing constructs — ``<link>``, ``<script>``,
``<iframe>``, ``srcset``, ``<picture><source>``, CSS ``url()``, and SVG
``<image>`` — are parsed as text or ignored entirely and produce no I/O.
Setting ``enable_remote_fetch=False`` is therefore sufficient to prevent all
outbound network activity from the converter; no pre-scrub step is required.

``HTMLBackendOptions`` has no ``local_fetch_root`` field.  Docling's local-path
read in ``_load_image_data`` opens files with a bare ``open(src_loc, "rb")``
and no containment check.  A path that traverses outside the workspace
(e.g. ``/workspace/../../etc/passwd``) is read unconditionally — subclassing
is the only way to enforce containment.

:func:`make_confined_backend` returns a ``HTMLDocumentBackend`` subclass that
closes over a workspace root and enforces:

* Remote fetches are rejected unconditionally (images must already be
  pre-fetched into the workspace by :func:`html_prefetch.prefetch_images`).
* Local path reads are resolved and checked against the workspace via
  :meth:`pathlib.Path.is_relative_to`.  Paths that escape raise
  :class:`WorkspaceEscape`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from docling.backend.html_backend import HTMLDocumentBackend

from aizk.conversion.core.errors import WorkspaceEscape

logger = logging.getLogger(__name__)


def make_confined_backend(workspace: Path) -> type[HTMLDocumentBackend]:
    """Return a ``HTMLDocumentBackend`` subclass confined to ``workspace``.

    The returned class closes over the resolved workspace root.  It overrides
    ``_load_image_data`` to enforce two invariants before delegating to the
    parent implementation:

    1. Remote URLs are always rejected — no outbound requests are issued.
    2. Local paths are resolved and compared against the workspace root; any
       path that falls outside raises :class:`WorkspaceEscape`.

    The workspace root is resolved once at factory-call time so that symlink
    races between construction and the containment check cannot widen the
    allowed set.

    Args:
        workspace: The directory to which all local image paths are confined.
            Must be an existing directory at the time of each ``_load_image_data``
            call, but need not exist at factory-call time.

    Returns:
        A fresh subclass of ``HTMLDocumentBackend`` with containment wired in.
        Each call returns a distinct class object suitable for use as
        ``HTMLFormatOption(backend=make_confined_backend(workspace), ...)``.
    """
    _workspace_resolved: Path = workspace.resolve()

    class _ConfinedHTMLDocumentBackend(HTMLDocumentBackend):
        """HTMLDocumentBackend with remote-fetch lockdown and workspace containment.

        ``HTMLBackendOptions`` has no ``local_fetch_root`` field; the parent's
        ``open()`` call in ``_load_image_data`` has no containment check.
        Subclassing is required to enforce workspace bounds.
        """

        def _load_image_data(self, src_loc: str) -> Optional[bytes]:
            """Load image data, blocking remote fetches and enforcing workspace containment.

            Remote URLs are silently dropped (logged at WARNING).  Local paths
            are resolved and validated against the workspace root before the
            parent implementation opens them.  ``data:`` URIs are delegated
            directly to the parent's base64-decode path without containment
            checks (they carry inline data, not filesystem paths).

            Args:
                src_loc: Resolved image src string as produced by
                    ``_resolve_relative_path``.

            Returns:
                Image bytes on success, ``None`` when the image is silently
                skipped (remote URL or missing/unreadable local file).

            Raises:
                WorkspaceEscape: When the resolved local path falls outside the
                    workspace root.
            """
            # Block remote fetches — images must be pre-fetched into the
            # workspace by html_prefetch.prefetch_images before Docling runs.
            if HTMLDocumentBackend._is_remote_url(src_loc):
                logger.warning(
                    "Blocked remote image fetch in ConfinedHTMLDocumentBackend",
                    extra={"src_loc": src_loc, "workspace": str(_workspace_resolved)},
                )
                return None

            # data: URIs carry inline content — delegate directly, no path check.
            if src_loc.startswith("data:"):
                return super()._load_image_data(src_loc)

            # Strip optional file:// prefix before path resolution.
            path_str = src_loc[7:] if src_loc.startswith("file://") else src_loc

            resolved = Path(path_str).resolve()
            if not resolved.is_relative_to(_workspace_resolved):
                raise WorkspaceEscape(
                    f"Image path resolves outside workspace: {resolved!s} is not within {_workspace_resolved!s}"
                )

            # Delegate the actual read to the parent (local-fetch guard + open).
            return super()._load_image_data(src_loc)

    _ConfinedHTMLDocumentBackend.__name__ = "ConfinedHTMLDocumentBackend"
    _ConfinedHTMLDocumentBackend.__qualname__ = "ConfinedHTMLDocumentBackend"
    return _ConfinedHTMLDocumentBackend


__all__ = ["make_confined_backend"]
