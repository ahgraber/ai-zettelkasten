"""Tests for the shared canonicalization and digest primitives."""

from __future__ import annotations

import hashlib
import json

from aizk.utilities.hashing import canonical_json, compute_markdown_hash, content_fingerprint, fingerprint

_NON_ASCII_PAYLOAD = {"b": 2, "a": "日本語 ✓"}


def test_canonical_json_sorts_keys_and_omits_whitespace() -> None:
    """The serialization is compact and key-ordered, so mapping order cannot move a digest."""
    ordered = canonical_json({"b": 2, "a": 1}, ensure_ascii=True)

    assert ordered == '{"a":1,"b":2}'
    assert canonical_json({"a": 1, "b": 2}, ensure_ascii=True) == ordered


def test_canonical_json_escapes_non_ascii_when_asked() -> None:
    """``ensure_ascii=True`` escapes; ``False`` emits the characters as-is."""
    escaped = canonical_json(_NON_ASCII_PAYLOAD, ensure_ascii=True)
    literal = canonical_json(_NON_ASCII_PAYLOAD, ensure_ascii=False)

    assert escaped != literal
    assert escaped.isascii()
    assert "日本語 ✓" in literal
    assert json.loads(escaped) == json.loads(literal) == _NON_ASCII_PAYLOAD


def test_canonical_json_ensure_ascii_changes_the_digest() -> None:
    """The two envelopes are different digest families over the same value."""
    escaped_digest = hashlib.sha256(canonical_json(_NON_ASCII_PAYLOAD, ensure_ascii=True).encode("utf-8")).hexdigest()
    literal_digest = hashlib.sha256(canonical_json(_NON_ASCII_PAYLOAD, ensure_ascii=False).encode("utf-8")).hexdigest()

    assert escaped_digest != literal_digest


def test_fingerprint_uses_the_unescaped_envelope() -> None:
    """``fingerprint`` is the full-width digest of the ``ensure_ascii=False`` form."""
    expected = hashlib.sha256(canonical_json(_NON_ASCII_PAYLOAD, ensure_ascii=False).encode("utf-8")).hexdigest()

    digest = fingerprint(_NON_ASCII_PAYLOAD)

    assert digest == expected
    assert len(digest) == 64


def test_content_fingerprint_digests_utf8_bytes() -> None:
    """``content_fingerprint`` hashes the text itself, with no JSON envelope."""
    text = "日本語 ✓"

    assert content_fingerprint(text) == hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_compute_markdown_hash_normalizes_line_endings_and_edges() -> None:
    """Line-ending and outer-whitespace differences do not move the artifact hash."""
    assert compute_markdown_hash("# Title\r\n\r\nBody\r\n") == compute_markdown_hash("\n# Title\n\nBody  ")
    assert len(compute_markdown_hash("# Title")) == 16
