"""Regression and version-discipline tests for the chunking splitter."""

from __future__ import annotations

from collections.abc import Callable
import json
import os
import re
import subprocess

import pytest

from aizk.chunking import SPLITTER_VERSION, Chunk, split
from aizk.utilities.path_utils import get_repo_path
from tests.chunking.conftest import FIXTURES_DIR

SNAPSHOT_PATH = FIXTURES_DIR / "expected_chunks.json"
_REPO_ROOT = get_repo_path(__file__)
_VERSION_FILE = "src/aizk/chunking/_version.py"
_SNAPSHOT_FILE = "tests/chunking/fixtures/expected_chunks.json"


def _build_snapshot(fixture_names: list[str], default_provenance: dict[str, str]) -> list[dict[str, object]]:
    """Build the (fixture -> chunks) snapshot at the default size budget."""
    snapshot: list[dict[str, object]] = []
    for name in fixture_names:
        text = (FIXTURES_DIR / name).read_text(encoding="utf-8")
        chunks = split(text, **default_provenance)
        # chunk_id is no longer a splitter output (identity is a persistence-assigned
        # surrogate), so the content snapshot captures only the fields split() produces.
        snapshot.append({"fixture": name, "chunks": [c.model_dump(mode="json", exclude={"chunk_id"}) for c in chunks]})
    return snapshot


def test_fixture_suite_snapshot(
    all_fixture_names: list[str],
    default_provenance: dict[str, str],
) -> None:
    """The (input -> chunks) mapping for the fixture suite does not drift.

    Regenerate intentionally with ``AIZK_UPDATE_CHUNK_SNAPSHOT=1``; any change to
    this snapshot must be accompanied by a ``SPLITTER_VERSION`` bump (enforced by
    :func:`test_version_bump_required_on_snapshot_change`).
    """
    current = _build_snapshot(all_fixture_names, default_provenance)

    if os.environ.get("AIZK_UPDATE_CHUNK_SNAPSHOT"):
        SNAPSHOT_PATH.write_text(json.dumps(current, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        pytest.skip("snapshot regenerated")

    assert SNAPSHOT_PATH.exists(), "run with AIZK_UPDATE_CHUNK_SNAPSHOT=1 to generate the snapshot"
    stored = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    assert current == stored


def _git_show(ref: str) -> str | None:
    """Return the content of ``ref`` from git, or None if unavailable."""
    try:
        completed = subprocess.run(  # noqa: S603
            ["git", "show", ref],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return completed.stdout


def _parse_splitter_version(source: str) -> int | None:
    """Extract the SPLITTER_VERSION integer from _version.py source."""
    match = re.search(r"SPLITTER_VERSION\s*:\s*int\s*=\s*(\d+)", source)
    return int(match.group(1)) if match else None


def test_version_bump_required_on_snapshot_change() -> None:
    """A changed snapshot without a SPLITTER_VERSION bump fails this check."""
    head_snapshot = _git_show(f"HEAD:{_SNAPSHOT_FILE}")
    if head_snapshot is None:
        pytest.skip("snapshot not yet committed; nothing to compare against HEAD")

    current_snapshot = SNAPSHOT_PATH.read_text(encoding="utf-8")
    if json.loads(head_snapshot) == json.loads(current_snapshot):
        return  # no drift, no bump required

    head_version_source = _git_show(f"HEAD:{_VERSION_FILE}")
    head_version = _parse_splitter_version(head_version_source or "")
    assert head_version is not None, "could not read SPLITTER_VERSION from HEAD"
    assert head_version != SPLITTER_VERSION, "fixture snapshot changed but SPLITTER_VERSION was not bumped"
