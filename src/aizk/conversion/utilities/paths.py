"""Path helpers for conversion artifacts."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from aizk.conversion.core.errors import WorkspaceEscape

logger = logging.getLogger(__name__)

OUTPUT_MARKDOWN_FILENAME = "output.md"
METADATA_FILENAME = "metadata.json"
FIGURES_DIRNAME = "figures"


def _assert_within(workspace: Path, name: str) -> Path:
    """Validate ``name`` and return a resolved path guaranteed to be inside ``workspace``.

    Applies a two-layer check:

    1. **String-level pre-check** — rejects names containing ``/``, ``\\``,
       or the bare traversal component ``..``, and rejects names that are
       absolute paths.  This catches the obvious attack forms before any
       filesystem access.

    2. **Path-level containment check** — composes ``workspace / name``,
       resolves it (following symlinks), and asserts the result is still
       inside ``workspace.resolve()``.  This catches symlinks that point
       outside the workspace and any edge cases the string check missed.

    Args:
        workspace: Root directory against which containment is enforced.
        name: Filename from subprocess metadata (caller-controlled).

    Returns:
        The validated absolute resolved path.

    Raises:
        WorkspaceEscape: If ``name`` fails either the string-level or
            path-level containment check.
    """
    if "/" in name or "\\" in name:
        logger.warning(
            "Workspace escape rejected: path separator in name",
            extra={"workspace": str(workspace), "path_name": name},
        )
        raise WorkspaceEscape(f"Unsafe path name contains separator: {name!r}")
    if os.path.isabs(name):
        logger.warning(
            "Workspace escape rejected: absolute path name",
            extra={"workspace": str(workspace), "path_name": name},
        )
        raise WorkspaceEscape(f"Absolute path not allowed: {name!r}")
    if name == "..":
        logger.warning(
            "Workspace escape rejected: bare traversal component",
            extra={"workspace": str(workspace), "path_name": name},
        )
        raise WorkspaceEscape(f"Path traversal component not allowed: {name!r}")

    composed = workspace / name
    try:
        resolved = composed.resolve()
    except Exception as exc:
        logger.warning(
            "Workspace escape rejected: path resolution failed",
            extra={"workspace": str(workspace), "path_name": name},
        )
        raise WorkspaceEscape(f"Could not resolve path {composed}") from exc

    if not resolved.is_relative_to(workspace.resolve()):
        logger.warning(
            "Workspace escape rejected: resolved path outside workspace",
            extra={"workspace": str(workspace.resolve()), "path_name": name, "resolved": str(resolved)},
        )
        raise WorkspaceEscape(f"Path {resolved} escapes workspace {workspace.resolve()}")
    return resolved


def metadata_path(workspace: Path) -> Path:
    """Return the path to the metadata JSON file."""
    return workspace / METADATA_FILENAME


def markdown_path(workspace: Path, filename: str = OUTPUT_MARKDOWN_FILENAME) -> Path:
    """Return the validated workspace-local path for the markdown artifact.

    Args:
        workspace: Per-job workspace directory.
        filename: Filename from subprocess metadata. Validated by
            :func:`_assert_within`; raises :class:`WorkspaceEscape` if the
            name attempts to traverse outside the workspace.

    Returns:
        Resolved absolute path inside ``workspace``.
    """
    return _assert_within(workspace, filename)


def figure_dir(workspace: Path) -> Path:
    """Return the path to the figures directory."""
    return workspace / FIGURES_DIRNAME


def figure_paths(workspace: Path, figure_files: list[str]) -> list[Path]:
    """Return validated workspace-local paths for figure artifacts.

    Each name in ``figure_files`` is validated by :func:`_assert_within`
    against the figures subdirectory.  Raises :class:`WorkspaceEscape` on
    the first name that fails containment.

    Args:
        workspace: Per-job workspace directory.
        figure_files: Figure filenames from subprocess metadata.

    Returns:
        List of resolved absolute paths, each inside
        ``workspace / FIGURES_DIRNAME``.
    """
    fdir = figure_dir(workspace)
    return [_assert_within(fdir, name) for name in figure_files]
