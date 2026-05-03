"""Unit tests for select_source_title and SubprocessMetadata round-trip."""

from __future__ import annotations


from pydantic import ValidationError
import pytest

from aizk.conversion.workers.types import SubprocessMetadata, _SourceMetaFields, select_source_title

# ---------------------------------------------------------------------------
# select_source_title
# ---------------------------------------------------------------------------

_MINIMAL_TERMINAL_REF = {"kind": "inline_html", "body": "dGVzdA=="}  # base64("test")


class TestSelectSourceTitle:
    """select_source_title heuristics."""

    def test_document_title_preferred_when_usable(self):
        result = select_source_title("Real Document Title", "Resolver Title")
        assert result == "Real Document Title"

    def test_uuid_32char_rejected(self):
        uuid_title = "a" * 32
        result = select_source_title(uuid_title, "Resolver fallback")
        assert result == "Resolver fallback"

    def test_uuid_36char_with_dashes_rejected(self):
        uuid_title = "12345678-1234-1234-1234-123456789012"
        result = select_source_title(uuid_title, "Resolver fallback")
        assert result == "Resolver fallback"

    def test_bare_http_url_rejected(self):
        result = select_source_title("http://example.com/something", "Resolver fallback")
        assert result == "Resolver fallback"

    def test_bare_https_url_rejected(self):
        result = select_source_title("https://example.com/page-title", "Resolver fallback")
        assert result == "Resolver fallback"

    def test_empty_string_rejected(self):
        result = select_source_title("", "Resolver fallback")
        assert result == "Resolver fallback"

    def test_whitespace_only_rejected(self):
        result = select_source_title("   ", "Resolver fallback")
        assert result == "Resolver fallback"

    def test_resolver_fallback_when_both_bad(self):
        result = select_source_title("http://example.com", "Resolver Title")
        assert result == "Resolver Title"

    def test_both_none_returns_none(self):
        result = select_source_title(None, None)
        assert result is None

    def test_both_bad_returns_none(self):
        result = select_source_title("", "")
        assert result is None

    def test_strips_whitespace_from_result(self):
        result = select_source_title("  My Title  ", None)
        assert result == "My Title"

    def test_non_uuid_hex_title_accepted(self):
        """A 10-char hex string is not UUID-shaped and should be accepted."""
        result = select_source_title("abcdef1234", None)
        assert result == "abcdef1234"


# ---------------------------------------------------------------------------
# SubprocessMetadata round-trip
# ---------------------------------------------------------------------------

_VALID_PAYLOAD = {
    "pipeline_name": "html",
    "terminal_ref": {"kind": "inline_html", "body": "dGVzdA=="},
    "content_type": "html",
    "markdown_filename": "output.md",
    "figure_files": [],
    "markdown_hash_xx64": "abc123",
    "docling_version": "2.0.0",
    "config_snapshot": {"converter_name": "docling"},
    "fetched_at": "2026-01-01T00:00:00+00:00",
    "source_meta": {
        "source_url": "https://example.com",
        "normalized_url": "https://example.com",
        "document_base_url": "https://example.com",
        "resolver_title": None,
    },
    "document_title": "My Document",
    "source_title": "My Document",
}


class TestSubprocessMetadata:
    """SubprocessMetadata validation and serialization."""

    def test_round_trip_valid_payload(self, tmp_path):
        meta = SubprocessMetadata.model_validate(_VALID_PAYLOAD)
        json_str = meta.model_dump_json()
        metadata_file = tmp_path / "metadata.json"
        metadata_file.write_text(json_str)
        restored = SubprocessMetadata.model_validate_json(metadata_file.read_text())
        assert restored.pipeline_name == "html"
        assert restored.source_meta.source_url == "https://example.com"
        assert restored.source_title == "My Document"
        assert restored.document_title == "My Document"

    def test_rejects_unknown_extra_field(self):
        bad = {**_VALID_PAYLOAD, "unexpected_field": "oops"}
        with pytest.raises(ValidationError):
            SubprocessMetadata.model_validate(bad)

    def test_rejects_missing_required_field(self):
        bad = {k: v for k, v in _VALID_PAYLOAD.items() if k != "pipeline_name"}
        with pytest.raises(ValidationError):
            SubprocessMetadata.model_validate(bad)

    def test_source_title_nullable(self):
        payload = {**_VALID_PAYLOAD, "source_title": None, "document_title": None}
        meta = SubprocessMetadata.model_validate(payload)
        assert meta.source_title is None
        assert meta.document_title is None

    def test_source_meta_fields_round_trip(self):
        from aizk.conversion.core.types import SourceMetadata

        original = SourceMetadata(
            source_url="https://example.com",
            normalized_url="https://example.com",
            document_base_url="https://example.com",
            resolver_title="My Title",
        )
        fields = _SourceMetaFields.from_source_metadata(original)
        restored = fields.to_source_metadata()
        assert restored == original
