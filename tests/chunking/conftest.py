"""Shared fixtures and helpers for chunking splitter tests."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from aizk.chunking import DEFAULT_SIZE_BUDGET, Chunk, split

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# Provenance context shared by tests; the cross-process determinism runner
# hard-codes these same values, so keep them in sync.
DOC_ID = "doc-1"
CONVERTED_ARTIFACT_ID = "artifact-1"
MARKDOWN_HASH_XX64 = "0123456789abcdef"


@pytest.fixture
def default_provenance() -> dict[str, str]:
    """Return the keyword provenance arguments accepted by :func:`split`."""
    return {
        "source_id": DOC_ID,
        "converted_artifact_id": CONVERTED_ARTIFACT_ID,
        "markdown_hash_xx64": MARKDOWN_HASH_XX64,
    }


@pytest.fixture
def load_fixture() -> Callable[[str], str]:
    """Return a loader that reads a fixture file's text by relative name."""

    def _load(name: str) -> str:
        return (FIXTURES_DIR / name).read_text(encoding="utf-8")

    return _load


@pytest.fixture
def do_split(load_fixture: Callable[[str], str], default_provenance: dict[str, str]) -> Callable[..., list[Chunk]]:
    """Return a helper that splits a named fixture with the shared provenance."""

    def _split(name: str, *, size_budget: int = DEFAULT_SIZE_BUDGET) -> list[Chunk]:
        return split(load_fixture(name), size_budget=size_budget, **default_provenance)

    return _split


@pytest.fixture
def all_fixture_names() -> list[str]:
    """Return every fixture markdown file's path relative to the fixtures dir."""
    names = [p.relative_to(FIXTURES_DIR).as_posix() for p in FIXTURES_DIR.rglob("*.md")]
    return sorted(names)
