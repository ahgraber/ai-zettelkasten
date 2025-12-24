"""Unit tests for hashing utilities."""

import hashlib

from aizk.conversion.utilities.hashing import compute_idempotency_key, compute_markdown_hash


def test_compute_idempotency_key_matches_sha256():
    key = compute_idempotency_key("uuid-1", 2, "2.65.0", "cfg")
    expected = hashlib.sha256("uuid-1:2:2.65.0:cfg".encode("utf-8")).hexdigest()
    assert key == expected


def test_compute_markdown_hash_normalizes_line_endings_and_trim():
    text_a = "Line 1\r\nLine 2\r\n"
    text_b = "Line 1\nLine 2"
    assert compute_markdown_hash(text_a) == compute_markdown_hash(text_b)
