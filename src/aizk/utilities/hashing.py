"""Shared content-hashing helpers used across pipeline stages.

The conversion stage publishes ``markdown_hash_xx64`` for converted artifacts and
the chunking stage derives ``content_hash`` for individual chunks; both reuse the
same normalization + xxh64 algorithm so identity is consistent across stages.

:func:`canonical_json` is the **compact** canonicalization only, and its
``ensure_ascii`` is a required per-call-site argument because the digest families
in this tree disagree on it. Three canonical-JSON sites are deliberately not this
function:

- ``aizk.chunking.datamodel.derive_chunk_content_key`` and the heading-path
  serialization in ``aizk.graph.chunks`` serialize JSON *arrays*, where
  ``sort_keys`` has nothing to sort and the envelope is positional.
- ``aizk.contextualization.core.keys._prompt_json`` uses the default separators
  because its output is the prompt string sent to the model; the compact form
  would change the prompt and therefore the model's output.
"""

from __future__ import annotations

import hashlib
import json

import xxhash


def compute_markdown_hash(markdown_text: str) -> str:
    """Compute the xxHash64 hex digest of normalized markdown content.

    Normalization mirrors the conversion stage's hash-time policy: CRLF / CR are
    folded to LF and the text is outer-stripped before encoding. This keeps the
    digest stable against line-ending differences and trailing-whitespace edits.

    Args:
        markdown_text: Markdown content to hash.

    Returns:
        Hex-encoded xxHash64 digest (16 characters).
    """
    normalized = markdown_text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return xxhash.xxh64(normalized.encode("utf-8")).hexdigest()


def canonical_json(value: object, *, ensure_ascii: bool) -> str:
    """Serialize ``value`` to the compact, key-sorted JSON a digest is taken over.

    Args:
        value: A JSON-serializable value.
        ensure_ascii: Whether non-ASCII characters are escaped. Required, and
            keyword-only, because it is part of each caller's digest envelope:
            flipping it changes every digest that site has ever produced.

    Returns:
        The serialized string: keys sorted, no whitespace between tokens.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=ensure_ascii)


def fingerprint(value: object) -> str:
    """Return the full-width SHA-256 hex digest of ``value``'s canonical JSON.

    Serializes ``value`` with sorted keys, compact separators, and no ASCII
    escaping, so a nested mapping's key order never affects the digest — the same
    canonicalization :func:`aizk.pipeline.identity.derivation_key` applies to its
    own envelope. Production code compares two ``fingerprint`` values for equality
    and never decodes one.

    Args:
        value: A JSON-serializable value (typically a mapping of a stage's
            declared inputs).

    Returns:
        The 64-character lowercase hex SHA-256 digest.
    """
    canonical = canonical_json(value, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def content_fingerprint(text: str) -> str:
    """Return the full-width SHA-256 hex digest of ``text``'s UTF-8 bytes.

    Always the full digest — never the 64-bit truncation some stages compute for
    a shorter change-detection hash (e.g. ``markdown_hash_xx64``) — so it is safe
    to use as an output-sensitive derivation-key input.

    Args:
        text: The text to fingerprint (e.g. a chunk's or a document's content).

    Returns:
        The 64-character lowercase hex SHA-256 digest.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
