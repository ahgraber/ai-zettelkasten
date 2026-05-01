"""Path helpers for conversion artifacts."""

from __future__ import annotations

import errno
import logging
import os
from pathlib import Path
import unicodedata

from aizk.conversion.core.errors import WorkspaceEscape

logger = logging.getLogger(__name__)

OUTPUT_MARKDOWN_FILENAME = "output.md"
METADATA_FILENAME = "metadata.json"
FIGURES_DIRNAME = "figures"


def _assert_within(workspace: Path, name: str) -> Path:
    r"""Validate ``name`` and return a resolved path guaranteed to be inside ``workspace``.

    Applies a two-layer check:

    1. **String-level pre-check** — rejects names containing ``/``, ``\\``,
       null bytes, or the bare traversal component ``..``; rejects names
       that are absolute paths; rejects names whose Unicode NFKC
       normalization introduces path separators or a bare ``..`` component
       (defends against homoglyph and full-width-character traversal
       attempts).  This catches the obvious attack forms before any
       filesystem access.

    2. **Path-level containment check** — composes ``workspace / name``,
       resolves it (following symlinks), and asserts the result is still
       inside ``workspace.resolve()``.  This catches symlinks that point
       outside the workspace and any edge cases the string check missed.

    **Residual TOCTOU note:** ``Path.resolve()`` and the subsequent
    ``open()`` call are not atomic.  ``O_NOFOLLOW`` at the open site
    (see :func:`read_text_nofollow` and ``_upload_nofollow`` in the
    uploader) closes the leaf-level race, but a malicious subprocess that
    can swap a *parent* directory for a symlink between this call and the
    ``open()`` could still redirect the read.  In the conversion threat
    model this is bounded by the converter subprocess being inside the
    workspace it would have to escape; full closure would require an
    ``openat``-style walk with ``O_PATH | O_NOFOLLOW`` on each segment,
    which Python does not expose portably.

    Args:
        workspace: Root directory against which containment is enforced.
        name: Filename from subprocess metadata (caller-controlled).

    Returns:
        The validated absolute resolved path.

    Raises:
        WorkspaceEscape: If ``name`` fails either the string-level or
            path-level containment check.
    """
    if "\x00" in name:
        logger.warning(
            "Workspace escape rejected: null byte in name",
            extra={"workspace": str(workspace), "path_name": repr(name)},
        )
        raise WorkspaceEscape(f"Null byte not allowed in path name: {name!r}")
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

    # Unicode NFKC normalization defeats homoglyph / full-width tricks
    # like "．．" (FULLWIDTH FULL STOP, U+FF0E ×2) which casefolds to "..",
    # or "／" (FULLWIDTH SOLIDUS, U+FF0F) which folds to "/". The path
    # would still be subject to the path-level containment check, but the
    # string check fails earlier with a clearer audit-log signal.
    normalized = unicodedata.normalize("NFKC", name)
    if normalized != name and (
        "/" in normalized or "\\" in normalized or normalized == ".." or os.path.isabs(normalized)
    ):
        logger.warning(
            "Workspace escape rejected: unicode normalization produces traversal",
            extra={"workspace": str(workspace), "path_name": name, "normalized": normalized},
        )
        raise WorkspaceEscape(f"Unicode-normalized name resolves to traversal: {name!r} -> {normalized!r}")

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


def read_text_nofollow(path: Path, *, encoding: str = "utf-8") -> str:
    """Read ``path`` as text using ``O_NOFOLLOW`` to reject leaf symlinks.

    Used at parent-side reads of subprocess-produced files (notably
    ``metadata.json``). Eliminates the TOCTOU window where a compromised
    converter subprocess swaps the validated file for a symlink between
    workspace creation and the parent's read: ``O_NOFOLLOW`` causes
    ``os.open`` to fail with ``ELOOP``, which is re-raised as
    :class:`WorkspaceEscape`.

    Args:
        path: Workspace-local path to read.
        encoding: Text decoding (default UTF-8).

    Returns:
        File contents as a decoded string.

    Raises:
        WorkspaceEscape: If the leaf is a symlink at open time (``ELOOP``).
        OSError: For other filesystem errors.
    """
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            logger.warning(
                "Workspace escape rejected: symlink detected at open time",
                extra={"path": str(path)},
            )
            raise WorkspaceEscape(f"Symlink detected at open time — possible TOCTOU: {path}") from exc
        raise
    with os.fdopen(fd, "r", encoding=encoding) as f:
        return f.read()


def write_text_nofollow(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    """Write ``content`` to ``path`` using ``O_NOFOLLOW | O_EXCL`` to refuse symlinks.

    Symmetric to :func:`read_text_nofollow` for parent-side writes that land
    inside a workspace shared with the conversion subprocess.  Used for
    parent-generated artifacts such as ``manifest.json``: a compromised
    converter subprocess could otherwise plant a symlink at the target path
    before exiting, causing the parent's write to clobber the symlink target.

    The flags ``O_WRONLY | O_CREAT | O_NOFOLLOW | O_EXCL`` collectively
    enforce:

    - ``O_NOFOLLOW`` — fail with ``ELOOP`` if the leaf is a symlink at open
      time; re-raised as :class:`WorkspaceEscape`.
    - ``O_EXCL`` — fail with ``EEXIST`` if the path already exists as a
      regular file.  The workspace is freshly created per job, so a
      pre-existing file at a parent-write target indicates the subprocess
      created one — refuse.

    Args:
        path: Workspace-local path to create.
        content: Text content to write.
        encoding: Text encoding (default UTF-8).

    Raises:
        WorkspaceEscape: If the leaf is a symlink at open time (``ELOOP``).
        FileExistsError: If a non-symlink already exists at ``path``.
        OSError: For other filesystem errors.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_EXCL
    try:
        fd = os.open(str(path), flags, 0o600)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            # Some platforms surface a symlink leaf as ELOOP with O_NOFOLLOW.
            logger.warning(
                "Workspace escape rejected: symlink detected at write open time",
                extra={"path": str(path)},
            )
            raise WorkspaceEscape(f"Symlink detected at open time — possible TOCTOU: {path}") from exc
        if exc.errno == errno.EEXIST:
            # POSIX: with O_CREAT | O_EXCL, an existing symlink also raises
            # EEXIST (not ELOOP). Distinguish via lstat for diagnostic clarity:
            # symlinks become WorkspaceEscape; pre-existing regular files
            # propagate as FileExistsError. Either way the write is refused.
            try:
                is_symlink = os.path.islink(str(path))
            except OSError:
                is_symlink = False
            if is_symlink:
                logger.warning(
                    "Workspace escape rejected: symlink detected at write open time",
                    extra={"path": str(path)},
                )
                raise WorkspaceEscape(f"Symlink detected at open time — possible TOCTOU: {path}") from exc
        raise
    with os.fdopen(fd, "w", encoding=encoding) as f:
        f.write(content)


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
