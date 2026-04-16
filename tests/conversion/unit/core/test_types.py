"""Tests for core conversion value types."""

from __future__ import annotations

from aizk.conversion.core.types import ContentType


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
    assert len(ContentType) == 7
