"""Unit tests for ContentType enum and SOURCE_TYPE_BY_KIND mapping."""

from __future__ import annotations

from typing import get_args

from pydantic import ValidationError
import pytest

from aizk.conversion.core.source_ref import SourceRef
from aizk.conversion.core.types import (
    SOURCE_TYPE_BY_KIND,
    ContentType,
    ConversionArtifacts,
    ConversionInput,
    SourceMetadata,
)


def test_content_type_has_seven_members():
    assert {ct.value for ct in ContentType} == {
        "pdf",
        "html",
        "image",
        "docx",
        "pptx",
        "xlsx",
        "csv",
    }


def test_content_type_is_str_enum():
    assert ContentType.PDF == "pdf"
    assert isinstance(ContentType.PDF, str)


def _variant_kinds_from_union() -> set[str]:
    """Derive SourceRef.kind literals from the discriminated union at test time."""
    union_args = get_args(get_args(SourceRef)[0])
    kinds: set[str] = set()
    for variant in union_args:
        kind_field = variant.model_fields["kind"]
        literal_args = get_args(kind_field.annotation)
        # kind: Literal["x"] -> get_args -> ("x",)
        assert len(literal_args) == 1, f"{variant} kind is not a single Literal"
        kinds.add(literal_args[0])
    return kinds


def test_source_type_by_kind_covers_every_variant():
    """Adding a new SourceRef variant without classifying it must fail."""
    variant_kinds = _variant_kinds_from_union()
    assert variant_kinds == set(SOURCE_TYPE_BY_KIND.keys())


def test_source_type_by_kind_values_are_classification_strings():
    # Classification values expected by storage/manifest layers.
    assert set(SOURCE_TYPE_BY_KIND.values()) <= {"arxiv", "github", "other"}
    assert SOURCE_TYPE_BY_KIND["arxiv"] == "arxiv"
    assert SOURCE_TYPE_BY_KIND["github_readme"] == "github"
    assert SOURCE_TYPE_BY_KIND["url"] == "other"
    assert SOURCE_TYPE_BY_KIND["karakeep_bookmark"] == "other"
    assert SOURCE_TYPE_BY_KIND["inline_html"] == "other"
    assert SOURCE_TYPE_BY_KIND["singlefile"] == "other"


def test_conversion_input_is_frozen():
    ci = ConversionInput(content=b"x", content_type=ContentType.PDF)
    with pytest.raises(ValidationError):
        ci.content = b"y"  # type: ignore[misc]


def test_conversion_input_defaults_source_meta_empty():
    ci = ConversionInput(content=b"x", content_type=ContentType.HTML)
    assert ci.source_meta == SourceMetadata()
    assert ci.source_meta.source_url is None


def test_conversion_artifacts_defaults_and_frozen():
    art = ConversionArtifacts(markdown="# hi")
    assert art.figures == []
    assert art.metadata == {}
    assert art.document_title is None
    with pytest.raises(ValidationError):
        art.markdown = "nope"  # type: ignore[misc]


def test_conversion_artifacts_document_title():
    art = ConversionArtifacts(markdown="# hi", document_title="My Title")
    assert art.document_title == "My Title"


# ---------------------------------------------------------------------------
# SourceMetadata.merge()
# ---------------------------------------------------------------------------


def test_source_metadata_merge_empty_plus_populated():
    empty = SourceMetadata()
    populated = SourceMetadata(source_url="https://example.com", resolver_title="My Title")
    result = empty.merge(populated)
    assert result.source_url == "https://example.com"
    assert result.resolver_title == "My Title"
    assert result.normalized_url is None
    assert result.document_base_url is None


def test_source_metadata_merge_earlier_wins():
    earlier = SourceMetadata(source_url="https://original.com", resolver_title="Resolver Title")
    later = SourceMetadata(source_url="https://asset.com", resolver_title="Later Title")
    result = earlier.merge(later)
    assert result.source_url == "https://original.com"
    assert result.resolver_title == "Resolver Title"


def test_source_metadata_merge_fills_missing_fields():
    partial = SourceMetadata(source_url="https://example.com")
    filler = SourceMetadata(normalized_url="https://example.com", document_base_url="https://example.com")
    result = partial.merge(filler)
    assert result.source_url == "https://example.com"
    assert result.normalized_url == "https://example.com"
    assert result.document_base_url == "https://example.com"


def test_source_metadata_merge_all_none():
    a = SourceMetadata()
    b = SourceMetadata()
    result = a.merge(b)
    assert result == SourceMetadata()


def test_source_metadata_is_frozen():
    meta = SourceMetadata(source_url="https://example.com")
    with pytest.raises(Exception):
        meta.source_url = "https://other.com"  # type: ignore[misc]
