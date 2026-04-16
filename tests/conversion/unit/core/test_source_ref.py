"""Tests for the SourceRef discriminated union."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

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


_adapter: TypeAdapter = TypeAdapter(SourceRef)


@pytest.mark.parametrize(
    "ref",
    [
        KarakeepBookmarkRef(bookmark_id="bk-1"),
        ArxivRef(arxiv_id="2301.12345"),
        ArxivRef(arxiv_id="2301.12345", arxiv_pdf_url="https://arxiv.org/pdf/2301.12345"),
        GithubReadmeRef(owner="anthropic", repo="claude-code"),
        UrlRef(url="https://example.com/page"),
        SingleFileRef(path="/tmp/page.html"),
        InlineHtmlRef(body=b"<html><body><pre>hello</pre></body></html>"),
    ],
)
def test_source_ref_json_round_trip(ref):
    encoded = _adapter.dump_json(ref)
    decoded = _adapter.validate_json(encoded)
    assert decoded == ref
    assert type(decoded) is type(ref)


def test_unknown_kind_rejected_on_deserialization():
    with pytest.raises(ValidationError):
        _adapter.validate_python({"kind": "not_a_kind", "url": "x"})


def test_inline_html_size_cap_enforced():
    body = b"x" * (64 * 1024 + 1)
    with pytest.raises(ValidationError):
        InlineHtmlRef(body=body)


def test_inline_html_at_size_cap_accepted():
    body = b"x" * (64 * 1024)
    ref = InlineHtmlRef(body=body)
    assert ref.body == body


def test_dedup_payload_ignores_cosmetic_fields():
    a = ArxivRef(arxiv_id="2301.12345")
    b = ArxivRef(arxiv_id="2301.12345", arxiv_pdf_url="https://arxiv.org/pdf/2301.12345")
    assert compute_source_ref_hash(a) == compute_source_ref_hash(b)


def test_dedup_payload_changes_with_identity_field():
    a = ArxivRef(arxiv_id="2301.12345")
    b = ArxivRef(arxiv_id="2301.99999")
    assert compute_source_ref_hash(a) != compute_source_ref_hash(b)


def test_inline_html_dedup_is_content_addressed():
    body = b"<html><body><pre>same content</pre></body></html>"
    a = InlineHtmlRef(body=body)
    b = InlineHtmlRef(body=body)
    assert compute_source_ref_hash(a) == compute_source_ref_hash(b)

    c = InlineHtmlRef(body=body + b" different")
    assert compute_source_ref_hash(a) != compute_source_ref_hash(c)


def test_inline_html_payload_is_hash_not_bytes():
    ref = InlineHtmlRef(body=b"hello")
    payload = ref.to_dedup_payload()
    assert payload["kind"] == "inline_html"
    assert "body" not in payload
    assert isinstance(payload["content_hash"], str) and len(payload["content_hash"]) == 64


def test_karakeep_payload_shape():
    ref = KarakeepBookmarkRef(bookmark_id="bk-1")
    assert ref.to_dedup_payload() == {"kind": "karakeep_bookmark", "bookmark_id": "bk-1"}


def test_url_payload_shape():
    ref = UrlRef(url="https://example.com/page")
    assert ref.to_dedup_payload() == {"kind": "url", "url": "https://example.com/page"}


def test_github_payload_shape():
    ref = GithubReadmeRef(owner="anthropic", repo="claude-code")
    assert ref.to_dedup_payload() == {
        "kind": "github_readme",
        "owner": "anthropic",
        "repo": "claude-code",
    }
