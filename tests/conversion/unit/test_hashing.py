"""Unit tests for hashing utilities."""

import hashlib

from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.utilities.hashing import compute_idempotency_key, compute_markdown_hash


def test_compute_idempotency_key_matches_sha256():
    from importlib.metadata import version
    import json

    config = ConversionConfig()
    config_json = json.dumps(
        {k: v for k, v in config.model_dump().items() if k.startswith("docling_")},
        sort_keys=True,
        separators=(",", ":"),
    )
    docling_version = version("docling")
    key = compute_idempotency_key("uuid-1", 2, config)
    expected = hashlib.sha256(f"uuid-1:2:{docling_version}:{config_json}".encode("utf-8")).hexdigest()
    assert key == expected


def test_compute_markdown_hash_normalizes_line_endings_and_trim():
    text_a = "Line 1\r\nLine 2\r\n"
    text_b = "Line 1\nLine 2"
    assert compute_markdown_hash(text_a) == compute_markdown_hash(text_b)
