"""Determinism and purity tests.

Requirement: Splitter is a deterministic pure function.
"""

from __future__ import annotations

import builtins
from collections.abc import Callable
import datetime as dt
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

from aizk.chunking import Chunk, split
from tests.chunking.conftest import (
    CONVERTED_ARTIFACT_ID,
    DOC_ID,
    MARKDOWN_HASH_XX64,
)


def test_two_invocations_field_equal(
    do_split: Callable[..., list[Chunk]],
) -> None:
    """Two invocations on the same input produce field-for-field equal chunks."""
    first = do_split("multi_section.md")
    second = do_split("multi_section.md")

    assert first == second


# Runs in a fresh interpreter: reads markdown on stdin, prints chunk_ids one per line.
_SUBPROCESS_RUNNER = (
    "import sys\n"
    "from aizk.chunking import split\n"
    "text = sys.stdin.read()\n"
    "chunks = split(text, source_id=%r, converted_artifact_id=%r, markdown_hash_xx64=%r)\n"
    "sys.stdout.write('\\n'.join(c.chunk_id for c in chunks))\n"
) % (DOC_ID, CONVERTED_ARTIFACT_ID, MARKDOWN_HASH_XX64)


def test_cross_process_chunk_ids_equal(
    load_fixture: Callable[[str], str],
    default_provenance: dict[str, str],
) -> None:
    """Independent processes derive identical chunk_id values from the same input."""
    text = load_fixture("multi_section.md")

    def run_subprocess() -> str:
        completed = subprocess.run(  # noqa: S603
            [sys.executable, "-c", _SUBPROCESS_RUNNER],
            input=text,
            capture_output=True,
            text=True,
            check=True,
        )
        return completed.stdout

    out_a = run_subprocess()
    out_b = run_subprocess()
    in_process = "\n".join(c.chunk_id for c in split(text, **default_provenance))

    assert out_a == out_b
    assert out_a == in_process


@pytest.mark.parametrize("fixture_name", ["multi_section.md", "oversize_paragraph.md"])
def test_no_io_during_split(
    fixture_name: str,
    load_fixture: Callable[[str], str],
    default_provenance: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """split() performs no filesystem, socket, or network I/O.

    Both the canonical path and the sentence-fallback path (chonkie) are covered.
    """
    text = load_fixture(fixture_name)  # read before patching out I/O primitives

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("split() must not perform I/O")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)

    chunks = split(text, size_budget=120, **default_provenance)

    assert chunks


def test_insensitive_to_env_and_time(
    load_fixture: Callable[[str], str],
    default_provenance: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Output is identical regardless of environment variables or wall-clock time."""
    text = load_fixture("multi_section.md")
    baseline = split(text, **default_provenance)

    for key, value in {
        "TZ": "Asia/Tokyo",
        "LANG": "xx_XX.UTF-8",
        "PYTHONHASHSEED": "12345",
        "AIZK_UNRELATED": "noise",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(time, "time", lambda: 1_234_567_890.0)
    monkeypatch.setattr(time, "monotonic", lambda: 42.0)

    fixed = dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)

    class _FrozenDateTime(dt.datetime):
        @classmethod
        def now(cls, tz: dt.tzinfo | None = None) -> dt.datetime:
            return fixed if tz is None else fixed.astimezone(tz)

    monkeypatch.setattr(dt, "datetime", _FrozenDateTime)

    after = split(text, **default_provenance)

    assert after == baseline
