"""Behavioral tests for the variant store's resolve-at-use selection."""

from __future__ import annotations

from aizk.graph.variants import ContextSource, ResolvedChunkText, resolve_chunk_text


def test_resolve_chunk_text_honors_toggle() -> None:
    """The toggle selects raw vs revised text and records which input was used."""
    revision = "The Transformer architecture builds on the prior result."

    raw = resolve_chunk_text("working text", contextualized_text=revision, contextualization_enabled=False)
    assert raw.source is ContextSource.RAW
    assert raw.text == "working text"

    ctx = resolve_chunk_text("working text", contextualized_text=revision, contextualization_enabled=True)
    assert ctx.source is ContextSource.CONTEXTUALIZED
    assert ctx.text == revision

    # With contextualization on but no variant available, the raw text is used.
    missing = resolve_chunk_text("working text", contextualized_text=None, contextualization_enabled=True)
    assert missing.source is ContextSource.RAW


def test_context_source_is_defined_in_the_variant_store() -> None:
    """The resolve-at-use types report the variant store as their defining module."""
    assert ContextSource.__module__ == "aizk.graph.variants"
    assert ResolvedChunkText.__module__ == "aizk.graph.variants"
