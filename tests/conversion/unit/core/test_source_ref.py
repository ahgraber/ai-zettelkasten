"""Unit tests for the SourceRef discriminated union and dedup-payload hashing."""

from __future__ import annotations

import hashlib
import json

from pydantic import TypeAdapter, ValidationError
import pytest

from aizk.conversion.core.source_ref import (
    ArxivRef,
    GithubReadmeRef,
    InlineHtmlRef,
    KarakeepBookmarkRef,
    SingleFileRef,
    SourceRef,
    UrlRef,
    compute_source_ref_hash,
)

_ADAPTER: TypeAdapter = TypeAdapter(SourceRef)


# --- JSON round-trip per variant --------------------------------------------


@pytest.mark.parametrize(
    "ref",
    [
        KarakeepBookmarkRef(bookmark_id="bk-001"),
        ArxivRef(arxiv_id="2301.12345"),
        ArxivRef(arxiv_id="2301.12345", arxiv_pdf_url="https://arxiv.org/pdf/2301.12345"),
        GithubReadmeRef(owner="ahgraber", repo="ai-zettelkasten"),
        GithubReadmeRef(owner="ahgraber", repo="ai-zettelkasten", branch="main"),
        UrlRef(url="https://example.com/foo"),
        SingleFileRef(url="https://example.com/a"),
        InlineHtmlRef(body=b"<html><body>hi</body></html>"),
    ],
)
def test_source_ref_json_round_trip(ref):
    encoded = _ADAPTER.dump_json(ref)
    decoded = _ADAPTER.validate_json(encoded)
    assert decoded == ref
    assert type(decoded) is type(ref)


def test_source_ref_unknown_kind_rejected():
    with pytest.raises(ValidationError):
        _ADAPTER.validate_python({"kind": "not-a-real-kind", "foo": "bar"})


# --- InlineHtmlRef size cap -------------------------------------------------


def test_inline_html_ref_accepts_body_at_cap():
    body = b"a" * (64 * 1024)
    ref = InlineHtmlRef(body=body)
    assert ref.body == body


def test_inline_html_ref_rejects_body_over_cap():
    body = b"a" * (64 * 1024 + 1)
    with pytest.raises(ValidationError):
        InlineHtmlRef(body=body)


def test_inline_html_ref_accepts_when_raw_fits_even_if_json_escape_exceeds_cap():
    # Bytes that JSON-escape to a MUCH longer string: control bytes (0x00-0x1F)
    # each escape to \uXXXX (6 chars). 12 KiB of such bytes yields a raw body
    # well under the cap but a JSON-escaped form well over it.
    body = bytes([1]) * (12 * 1024)
    assert len(body) < 64 * 1024
    ref = InlineHtmlRef(body=body)
    encoded = _ADAPTER.dump_json(ref)
    assert len(encoded) > 64 * 1024
    assert ref.body == body


def test_inline_html_ref_rejects_when_raw_exceeds_cap_even_if_short_when_encoded():
    # All ASCII bytes encode 1:1 as JSON bytes (plus quotes + base64/ascii wrap).
    # A raw 64 KiB + 1 body must be rejected regardless of how the encoded
    # representation compares — the check is on raw body length.
    body = b"A" * (64 * 1024 + 1)
    with pytest.raises(ValidationError):
        InlineHtmlRef(body=body)


# --- to_dedup_payload: cosmetic-equivalence vs identity-difference -----------


def test_arxiv_dedup_ignores_arxiv_pdf_url():
    a = ArxivRef(arxiv_id="2301.12345")
    b = ArxivRef(arxiv_id="2301.12345", arxiv_pdf_url="https://arxiv.org/pdf/2301.12345")
    assert compute_source_ref_hash(a) == compute_source_ref_hash(b)


def test_arxiv_dedup_normalizes_whitespace():
    a = ArxivRef(arxiv_id="2301.12345")
    b = ArxivRef(arxiv_id="  2301.12345  ")
    assert compute_source_ref_hash(a) == compute_source_ref_hash(b)


def test_arxiv_dedup_identity_differs():
    a = ArxivRef(arxiv_id="2301.12345")
    b = ArxivRef(arxiv_id="2401.99999")
    assert compute_source_ref_hash(a) != compute_source_ref_hash(b)


def test_github_dedup_ignores_branch():
    a = GithubReadmeRef(owner="ahgraber", repo="ai-zettelkasten")
    b = GithubReadmeRef(owner="ahgraber", repo="ai-zettelkasten", branch="main")
    assert compute_source_ref_hash(a) == compute_source_ref_hash(b)


def test_github_dedup_identity_differs_on_repo():
    a = GithubReadmeRef(owner="ahgraber", repo="ai-zettelkasten")
    b = GithubReadmeRef(owner="ahgraber", repo="other-repo")
    assert compute_source_ref_hash(a) != compute_source_ref_hash(b)


def test_url_dedup_normalizes_cosmetic_variation():
    a = UrlRef(url="https://example.com/foo")
    b = UrlRef(url="HTTPS://Example.COM/foo/")
    c = UrlRef(url="https://example.com/foo?utm_source=x")
    assert compute_source_ref_hash(a) == compute_source_ref_hash(b) == compute_source_ref_hash(c)


def test_url_dedup_identity_differs():
    a = UrlRef(url="https://example.com/a")
    b = UrlRef(url="https://example.com/b")
    assert compute_source_ref_hash(a) != compute_source_ref_hash(b)


def test_karakeep_dedup_identity_differs():
    a = KarakeepBookmarkRef(bookmark_id="bk-001")
    b = KarakeepBookmarkRef(bookmark_id="bk-002")
    assert compute_source_ref_hash(a) != compute_source_ref_hash(b)


def test_inline_html_dedup_is_content_addressed():
    body = b"<html><body>hi</body></html>"
    a = InlineHtmlRef(body=body)
    b = InlineHtmlRef(body=body)
    assert compute_source_ref_hash(a) == compute_source_ref_hash(b)


def test_inline_html_dedup_payload_does_not_embed_bytes():
    payload = InlineHtmlRef(body=b"<html><body>hi</body></html>").to_dedup_payload()
    assert "body" not in payload
    assert payload["content_hash"] == hashlib.sha256(b"<html><body>hi</body></html>").hexdigest()


def test_inline_html_dedup_hash_differs_on_body_change():
    a = InlineHtmlRef(body=b"<html>a</html>")
    b = InlineHtmlRef(body=b"<html>b</html>")
    assert compute_source_ref_hash(a) != compute_source_ref_hash(b)


def test_compute_hash_matches_manual_json_encoding():
    # The helper must match the documented canonical encoding so callers that
    # persist the payload directly can reproduce the hash.
    ref = ArxivRef(arxiv_id="2301.12345")
    manual = hashlib.sha256(
        json.dumps(ref.to_dedup_payload(), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert compute_source_ref_hash(ref) == manual


# --- Fixture-lock: pinned (ref, expected sha256) pairs -----------------------

_PINNED_FIXTURES = [
    # karakeep
    (
        KarakeepBookmarkRef(bookmark_id="bk-001"),
        "a6b8adbabffe16d4207d1de5a51e96dc4b3171bd0f959233075a1fd0c51072b0",
    ),
    # arxiv — plain
    (
        ArxivRef(arxiv_id="2301.12345"),
        "2d9b0a6480cb12661725ffbb0a53ff876f2428d569f404d6693968cb0c3e942f",
    ),
    # arxiv — cosmetic pdf_url must NOT affect the hash
    (
        ArxivRef(arxiv_id="2301.12345", arxiv_pdf_url="https://arxiv.org/pdf/2301.12345"),
        "2d9b0a6480cb12661725ffbb0a53ff876f2428d569f404d6693968cb0c3e942f",
    ),
    # arxiv — whitespace normalization
    (
        ArxivRef(arxiv_id="  2301.12345  "),
        "2d9b0a6480cb12661725ffbb0a53ff876f2428d569f404d6693968cb0c3e942f",
    ),
    # github — no branch
    (
        GithubReadmeRef(owner="ahgraber", repo="ai-zettelkasten"),
        "e5d35ddc1928378e94ad6b3166cae35bebf8f9b38871c2b38c5a2b7660891bdf",
    ),
    # github — branch must NOT affect the hash
    (
        GithubReadmeRef(owner="ahgraber", repo="ai-zettelkasten", branch="main"),
        "e5d35ddc1928378e94ad6b3166cae35bebf8f9b38871c2b38c5a2b7660891bdf",
    ),
    # url — plain (already normalized)
    (
        UrlRef(url="https://example.com/foo"),
        "67b168456ce39850029f94a1e4f7f468dadf90d81914eb612e33320d833fe352",
    ),
    # url — pre-normalization form (case, trailing slash) produces the same hash
    (
        UrlRef(url="HTTPS://Example.COM/foo/"),
        "67b168456ce39850029f94a1e4f7f468dadf90d81914eb612e33320d833fe352",
    ),
    # url — utm stripping produces the same hash
    (
        UrlRef(url="https://example.com/foo?utm_source=x"),
        "67b168456ce39850029f94a1e4f7f468dadf90d81914eb612e33320d833fe352",
    ),
    # singlefile
    (
        SingleFileRef(url="https://example.com/a"),
        "dd2f9da526d3bad9054ef25e65e6fbfea1bed366c5bc682cb3a917b4ff23998f",
    ),
    # inline html — short body
    (
        InlineHtmlRef(body=b"<html><body>hi</body></html>"),
        "044e83b4090298e061b1c94d244c300770735e3e2d3f9128d6582f4c661c2fe8",
    ),
    # inline html — empty body (edge case)
    (
        InlineHtmlRef(body=b""),
        "86d95fb5c7761f60e8e142dd9cbaaa6918b7305925471b15c00aa1fb7b6fcbaf",
    ),
]


@pytest.mark.parametrize("ref,expected_hash", _PINNED_FIXTURES)
def test_dedup_payload_fixture_lock(ref, expected_hash):
    """Fail loudly on any change to `to_dedup_payload()` — either revert the change or ship a data migration."""
    assert compute_source_ref_hash(ref) == expected_hash
