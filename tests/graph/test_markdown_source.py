"""Tests for the production S3-backed MarkdownSource.

Resolves a ``conversion_output_id`` to the recorded blob key + markdown hash and
reads the Markdown through an injected blob reader (a fake here). FK enforcement
is left off (no pragma), so a standalone ``conversion_outputs`` row suffices.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from sqlmodel import Session, create_engine

from aizk.conversion.datamodel.output import ConversionOutput
from aizk.graph.markdown_source import S3MarkdownSource
from aizk.utilities.hashing import compute_markdown_hash

_MARKDOWN = "# Title\n\nSome body text for the source document.\n"
_AIZK_UUID = UUID("11111111-1111-1111-1111-111111111111")


class _FakeBlobReader:
    """In-memory blob reader keyed by storage key."""

    def __init__(self, blobs: dict[str, bytes]) -> None:
        self._blobs = blobs

    def get_object_bytes(self, s3_key: str) -> bytes:
        return self._blobs[s3_key]


def _engine_with_output(tmp_path: Path, *, markdown_hash: str):
    """Build an engine with one conversion_outputs row; return (engine, output_id)."""
    engine = create_engine(f"sqlite:///{tmp_path / 'md.db'}")
    ConversionOutput.__table__.create(engine)
    with Session(engine) as session:
        output = ConversionOutput(
            job_id=1,
            source_id=_AIZK_UUID,
            owner_id="owner",
            title="Doc",
            payload_version=1,
            s3_prefix="prefix",
            markdown_key="prefix/output.md",
            manifest_key="prefix/manifest.json",
            markdown_hash_xx64=markdown_hash,
            docling_version="1.0",
            pipeline_name="docling",
        )
        session.add(output)
        session.commit()
        output_id = output.id
    return engine, output_id


def test_load_returns_markdown_text_and_recorded_hash(tmp_path: Path) -> None:
    """The source reads the blob at the output's markdown_key and returns its recorded hash."""
    markdown_hash = compute_markdown_hash(_MARKDOWN)
    engine, output_id = _engine_with_output(tmp_path, markdown_hash=markdown_hash)
    reader = _FakeBlobReader({"prefix/output.md": _MARKDOWN.encode("utf-8")})

    loaded = S3MarkdownSource(engine, reader).load(output_id)

    assert loaded.text == _MARKDOWN
    assert loaded.markdown_hash_xx64 == markdown_hash


def test_load_rejects_unknown_output(tmp_path: Path) -> None:
    """A locator with no conversion output row is rejected before any blob read."""
    engine, _ = _engine_with_output(tmp_path, markdown_hash="0011223344556677")
    source = S3MarkdownSource(engine, _FakeBlobReader({}))

    with pytest.raises(ValueError, match="conversion output 999 not found"):
        source.load(999)
