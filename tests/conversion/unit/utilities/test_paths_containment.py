"""Unit tests for workspace path-containment helpers in ``paths.py``."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from aizk.conversion.core.errors import WorkspaceEscape
from aizk.conversion.utilities.paths import (
    METADATA_FILENAME,
    _assert_within,
    figure_paths,
    markdown_path,
    metadata_path,
    read_text_nofollow,
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
        "/tmp/secret",  # noqa: S108
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


@pytest.mark.parametrize(
    "name",
    [
        "innocent\x00.png",
        "\x00leading-null.md",
        "trailing-null.md\x00",
    ],
)
def test_assert_within_rejects_null_byte_names(tmp_path: Path, name: str) -> None:
    # Null bytes in names previously passed string-level checks and reached
    # `composed.resolve()`, where they failed with a generic `path resolution
    # failed` log line. Reject them explicitly so the audit-log signal is clean.
    with pytest.raises(WorkspaceEscape):
        _assert_within(tmp_path, name)


@pytest.mark.parametrize(
    "name",
    [
        # FULLWIDTH FULL STOP × 2 → ".." after NFKC normalization
        "．．",
        # FULLWIDTH SOLIDUS introducing a path separator after normalization
        "innocent／path.md",
    ],
)
def test_assert_within_rejects_unicode_traversal_lookalikes(tmp_path: Path, name: str) -> None:
    # NFKC normalization defeats homoglyph traversal attempts that would
    # otherwise pass the string-level pre-check on bytes-level inspection but
    # produce ".." or "/" once normalized.
    with pytest.raises(WorkspaceEscape):
        _assert_within(tmp_path, name)


def test_assert_within_accepts_unicode_filename_that_normalizes_to_safe(tmp_path: Path) -> None:
    # Plain Unicode characters that NFKC-normalize to other plain characters
    # (e.g., a non-Latin filename) must not be rejected — only normalizations
    # that introduce traversal components are blocked.
    name = "étoile.md"  # "étoile.md"
    result = _assert_within(tmp_path, name)
    assert result == (tmp_path / name).resolve()


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


# ---------------------------------------------------------------------------
# read_text_nofollow — defeats post-validation symlink swap on metadata.json
# ---------------------------------------------------------------------------


def test_read_text_nofollow_returns_contents_for_regular_file(tmp_path: Path) -> None:
    target = tmp_path / "metadata.json"
    target.write_text('{"ok": true}', encoding="utf-8")
    assert read_text_nofollow(target) == '{"ok": true}'


def test_read_text_nofollow_rejects_symlink_with_workspace_escape(tmp_path: Path) -> None:
    # Regression for H4: a compromised converter subprocess that swaps
    # workspace/metadata.json for a symlink must fail at parent-side read,
    # not silently feed attacker-chosen JSON into the manifest / DB / S3.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text('{"poisoned": true}', encoding="utf-8")
    (workspace / METADATA_FILENAME).symlink_to(outside)

    with pytest.raises(WorkspaceEscape):
        read_text_nofollow(metadata_path(workspace))


def test_read_text_nofollow_propagates_filesystem_errors(tmp_path: Path) -> None:
    # Non-ELOOP errors (e.g. ENOENT) propagate as plain OSError, not WorkspaceEscape.
    missing = tmp_path / "does-not-exist.json"
    with pytest.raises(FileNotFoundError):
        read_text_nofollow(missing)


# ---------------------------------------------------------------------------
# write_text_nofollow — refuses to follow symlinks on parent-side writes
# ---------------------------------------------------------------------------


def test_write_text_nofollow_writes_new_file(tmp_path: Path) -> None:
    """A regular new-file write succeeds and returns the validated bytes."""
    from aizk.conversion.utilities.paths import write_text_nofollow

    target = tmp_path / "manifest.json"
    write_text_nofollow(target, '{"version": "2.0"}')
    assert target.read_text(encoding="utf-8") == '{"version": "2.0"}'


def test_write_text_nofollow_rejects_existing_symlink_with_workspace_escape(tmp_path: Path) -> None:
    """Regression for H2: a subprocess that pre-creates ``manifest.json`` as a symlink
    must NOT cause the parent's ``save_manifest`` to write through it.

    Threat model: the conversion subprocess (compromised in the design's threat
    model) plants ``<workspace>/manifest.json`` as a symlink to any host-writable
    file before exiting. Without ``O_NOFOLLOW``, the parent's ``write_text`` would
    overwrite the symlink target with manifest JSON — an arbitrary-file overwrite
    primitive identical-shape to the H4 fix already shipped for markdown/figures
    /``metadata.json``.
    """
    from aizk.conversion.utilities.paths import write_text_nofollow

    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text('{"untouched": true}', encoding="utf-8")
    (workspace / "manifest.json").symlink_to(outside)

    with pytest.raises(WorkspaceEscape, match="Symlink detected"):
        write_text_nofollow(workspace / "manifest.json", '{"poisoned": "yes"}')

    # The symlink target must NOT have been written.
    assert outside.read_text(encoding="utf-8") == '{"untouched": true}'


def test_write_text_nofollow_rejects_existing_regular_file_by_default(tmp_path: Path) -> None:
    """Defense-in-depth: by default, refuse to overwrite a pre-existing file.

    The workspace is freshly created per job; a pre-existing ``manifest.json``
    indicates the subprocess wrote one (it shouldn't — manifest is parent-only).
    Refusing the write closes one variant of subprocess-supplied content
    flowing into the manifest path.
    """
    from aizk.conversion.utilities.paths import write_text_nofollow

    target = tmp_path / "manifest.json"
    target.write_text("preexisting", encoding="utf-8")

    with pytest.raises(FileExistsError):
        write_text_nofollow(target, '{"new": true}')

    # Pre-existing content untouched.
    assert target.read_text(encoding="utf-8") == "preexisting"


def test_write_text_nofollow_propagates_filesystem_errors(tmp_path: Path) -> None:
    """Non-ELOOP errors (e.g. ENOENT for parent dir) propagate as plain OSError."""
    from aizk.conversion.utilities.paths import write_text_nofollow

    bad = tmp_path / "no-such-dir" / "manifest.json"
    with pytest.raises(FileNotFoundError):
        write_text_nofollow(bad, "x")
