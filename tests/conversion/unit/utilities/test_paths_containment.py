"""Unit tests for workspace path-containment helpers in ``paths.py``."""

from __future__ import annotations

import errno
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from aizk.conversion.core.errors import WorkspaceEscape
from aizk.conversion.utilities.paths import (
    _assert_within,
    figure_paths,
    markdown_path,
)
from aizk.conversion.workers.uploader import _upload_nofollow

# ---------------------------------------------------------------------------
# _assert_within — string-level rejections
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "../../etc/hostname",
        "../sibling",
        "subdir/file.txt",
        "subdir/../file.txt",
    ],
)
def test_assert_within_rejects_names_with_forward_slash(tmp_path: Path, name: str) -> None:
    with pytest.raises(WorkspaceEscape):
        _assert_within(tmp_path, name)


@pytest.mark.parametrize(
    "name",
    [
        "/etc/hostname",
        "/tmp/secret",
    ],
)
def test_assert_within_rejects_absolute_paths(tmp_path: Path, name: str) -> None:
    with pytest.raises(WorkspaceEscape):
        _assert_within(tmp_path, name)


def test_assert_within_rejects_backslash_traversal(tmp_path: Path) -> None:
    # Raw string value is "..\\..\etc\hostname" — contains backslashes.
    name = "..\\..\\etc\\hostname"
    with pytest.raises(WorkspaceEscape):
        _assert_within(tmp_path, name)


def test_assert_within_rejects_bare_dotdot(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceEscape):
        _assert_within(tmp_path, "..")


# ---------------------------------------------------------------------------
# _assert_within — accepted names
# ---------------------------------------------------------------------------


def test_assert_within_accepts_simple_markdown_filename(tmp_path: Path) -> None:
    result = _assert_within(tmp_path, "output.md")
    assert result == (tmp_path / "output.md").resolve()


def test_assert_within_accepts_figure_filename(tmp_path: Path) -> None:
    result = _assert_within(tmp_path, "figure-001.png")
    assert result == (tmp_path / "figure-001.png").resolve()


def test_assert_within_returns_resolved_absolute_path(tmp_path: Path) -> None:
    result = _assert_within(tmp_path, "output.md")
    assert result.is_absolute()


# ---------------------------------------------------------------------------
# _assert_within — path-level symlink containment
# ---------------------------------------------------------------------------


def test_assert_within_rejects_symlink_pointing_outside(tmp_path: Path) -> None:
    """A symlink inside the workspace that resolves outside is rejected."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    # Create a symlink inside workspace that points to a directory outside.
    escape_link = workspace / "escape"
    escape_link.symlink_to(tmp_path)  # points to parent, which is outside workspace

    with pytest.raises(WorkspaceEscape):
        _assert_within(workspace, "escape")


# ---------------------------------------------------------------------------
# markdown_path and figure_paths delegate to _assert_within
# ---------------------------------------------------------------------------


def test_markdown_path_rejects_traversal(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceEscape):
        markdown_path(tmp_path, "../../etc/passwd")


def test_markdown_path_accepts_safe_name(tmp_path: Path) -> None:
    result = markdown_path(tmp_path, "output.md")
    assert result == (tmp_path / "output.md").resolve()


def test_figure_paths_rejects_traversal_in_any_entry(tmp_path: Path) -> None:
    (tmp_path / "figures").mkdir()
    with pytest.raises(WorkspaceEscape):
        figure_paths(tmp_path, ["figure-001.png", "../../etc/passwd"])


def test_figure_paths_accepts_safe_names(tmp_path: Path) -> None:
    (tmp_path / "figures").mkdir()
    result = figure_paths(tmp_path, ["figure-001.png", "figure-002.png"])
    figures_dir = (tmp_path / "figures").resolve()
    assert result == [figures_dir / "figure-001.png", figures_dir / "figure-002.png"]


# ---------------------------------------------------------------------------
# _upload_nofollow — O_NOFOLLOW catches symlink swap (TOCTOU mitigation)
# ---------------------------------------------------------------------------


def test_upload_nofollow_raises_workspace_escape_on_symlink(tmp_path: Path) -> None:
    """A path that becomes a symlink after validation raises WorkspaceEscape."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    # Create a real file; caller validates it via _assert_within.
    real_file = workspace / "output.md"
    real_file.write_text("content")

    validated_path = _assert_within(workspace, "output.md")

    # Simulate the TOCTOU race: subprocess replaces the file with a symlink.
    real_file.unlink()
    real_file.symlink_to("/etc/passwd")

    mock_client = MagicMock()
    with pytest.raises(WorkspaceEscape, match="Symlink detected"):
        _upload_nofollow(validated_path, "prefix/output.md", mock_client)

    # The S3 client must NOT have been called.
    mock_client.upload_fileobj.assert_not_called()


def test_upload_nofollow_uploads_regular_file(tmp_path: Path) -> None:
    """A regular file is opened and passed to upload_fileobj."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    content = b"# Hello"
    real_file = workspace / "output.md"
    real_file.write_bytes(content)

    validated_path = _assert_within(workspace, "output.md")

    # Capture the file content during the call — the fd is closed when
    # _upload_nofollow returns so we cannot read it after the fact.
    captured: list[bytes] = []

    def _capture(file_obj, s3_key):
        captured.append(file_obj.read())
        return "s3://bucket/prefix/output.md"

    mock_client = MagicMock()
    mock_client.upload_fileobj.side_effect = _capture

    uri = _upload_nofollow(validated_path, "prefix/output.md", mock_client)

    assert uri == "s3://bucket/prefix/output.md"
    mock_client.upload_fileobj.assert_called_once()
    assert captured == [content]


# ---------------------------------------------------------------------------
# Happy-path: standard subprocess metadata flows through without errors
# ---------------------------------------------------------------------------


def test_standard_subprocess_metadata_accepted(tmp_path: Path) -> None:
    """Safe filenames from subprocess metadata resolve without WorkspaceEscape."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "figures").mkdir()

    md_path = markdown_path(workspace, "output.md")
    fig_paths = figure_paths(workspace, ["figure-001.png", "figure-002.png"])

    assert md_path == (workspace / "output.md").resolve()
    assert fig_paths == [
        (workspace / "figures" / "figure-001.png").resolve(),
        (workspace / "figures" / "figure-002.png").resolve(),
    ]
