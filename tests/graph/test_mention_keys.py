"""Mention-store key-derivation tests.

Requirement: an extraction run's derivation key is a function of exactly two
declared inputs — the payload extraction actually read and the
currently-resolved extraction configuration — changing when either changes,
with no local surrogate id (a ``run_id`` or row id) admitted into either it or
a source-anchored mention's ``source_occurrence_key``. These guard the two
portability properties the mention store's idempotency and cross-run diffing
rely on: recomputing a key from the same inputs on any process or backend
yields the same value, and the ``source_occurrence_key`` needs no run to be
computed.
"""

from __future__ import annotations

import inspect

import pytest

from aizk.graph.datamodel import INPUT_KIND_CONTEXTUALIZED, INPUT_KIND_RAW
from aizk.graph.fingerprints import PayloadItem, payload_fingerprint
from aizk.graph.mentions import (
    ResolvedExtractionConfig,
    derive_source_occurrence_key,
    extraction_derivation_key,
)
from aizk.pipeline.identity import derivation_key
from aizk.utilities.hashing import content_fingerprint

_BASE_CONFIG = ResolvedExtractionConfig(
    extractor_version="spacy/en_core_web_sm@3.7", materializer_version="1", input_policy="raw"
)


def _payload(
    *,
    text: str = "Ada Lovelace wrote the first algorithm.",
    chunk_key: str = "chunk-1",
    input_kind: str = INPUT_KIND_RAW,
) -> str:
    """Build a one-item payload fingerprint, the shape :func:`extraction_derivation_key` consumes."""
    return payload_fingerprint([PayloadItem(chunk_key, input_kind, content_fingerprint(text))])


def test_extraction_key_deterministic_for_same_inputs() -> None:
    """Identical semantic inputs derive an identical extraction derivation key."""
    payload = _payload()
    config = _BASE_CONFIG.config_fingerprint()

    first = extraction_derivation_key(input_fingerprint=payload, config_fingerprint=config)
    second = extraction_derivation_key(input_fingerprint=payload, config_fingerprint=config)

    assert first == second


def test_extraction_key_changes_when_input_text_changes() -> None:
    """A changed chunk text changes the payload fingerprint and therefore the key."""
    config = _BASE_CONFIG.config_fingerprint()
    base = extraction_derivation_key(
        input_fingerprint=_payload(text="Ada Lovelace wrote the first algorithm."), config_fingerprint=config
    )
    changed = extraction_derivation_key(
        input_fingerprint=_payload(text="Something else entirely."), config_fingerprint=config
    )

    assert base != changed


def test_extraction_key_changes_when_input_policy_changes() -> None:
    """A changed ``input_policy`` changes the config fingerprint and therefore the key, payload held fixed."""
    payload = _payload()
    raw = ResolvedExtractionConfig(
        extractor_version=_BASE_CONFIG.extractor_version,
        materializer_version=_BASE_CONFIG.materializer_version,
        input_policy="raw",
    )
    contextualized = ResolvedExtractionConfig(
        extractor_version=_BASE_CONFIG.extractor_version,
        materializer_version=_BASE_CONFIG.materializer_version,
        input_policy="contextualized",
    )

    key_raw = extraction_derivation_key(input_fingerprint=payload, config_fingerprint=raw.config_fingerprint())
    key_contextualized = extraction_derivation_key(
        input_fingerprint=payload, config_fingerprint=contextualized.config_fingerprint()
    )

    assert key_raw != key_contextualized


def test_extraction_key_changes_when_extractor_version_changes() -> None:
    """A changed ``extractor_version`` changes the config fingerprint and therefore the key, payload held fixed."""
    payload = _payload()
    other = ResolvedExtractionConfig(
        extractor_version="gliner2==1.0/fastino/gliner2-base-v1@abc",
        materializer_version=_BASE_CONFIG.materializer_version,
        input_policy=_BASE_CONFIG.input_policy,
    )

    base_key = extraction_derivation_key(
        input_fingerprint=payload, config_fingerprint=_BASE_CONFIG.config_fingerprint()
    )
    other_key = extraction_derivation_key(input_fingerprint=payload, config_fingerprint=other.config_fingerprint())

    assert base_key != other_key


def test_extraction_key_changes_when_materializer_version_changes() -> None:
    """A changed ``materializer_version`` changes the config fingerprint and therefore the key, payload held fixed."""
    payload = _payload()
    other = ResolvedExtractionConfig(
        extractor_version=_BASE_CONFIG.extractor_version,
        materializer_version="2",
        input_policy=_BASE_CONFIG.input_policy,
    )

    base_key = extraction_derivation_key(
        input_fingerprint=payload, config_fingerprint=_BASE_CONFIG.config_fingerprint()
    )
    other_key = extraction_derivation_key(input_fingerprint=payload, config_fingerprint=other.config_fingerprint())

    assert base_key != other_key


def test_extraction_key_distinguishes_duplicate_chunk_text_at_two_positions() -> None:
    """Duplicate chunk text at a second position is a distinct payload, so removing or reordering it changes the key."""
    config = _BASE_CONFIG.config_fingerprint()
    single = payload_fingerprint([PayloadItem("chunk-1", INPUT_KIND_RAW, content_fingerprint("Acme Corp."))])
    duplicated = payload_fingerprint(
        [
            PayloadItem("chunk-1", INPUT_KIND_RAW, content_fingerprint("Acme Corp.")),
            PayloadItem("chunk-2", INPUT_KIND_RAW, content_fingerprint("Acme Corp.")),
        ]
    )
    reordered = payload_fingerprint(
        [
            PayloadItem("chunk-2", INPUT_KIND_RAW, content_fingerprint("Acme Corp.")),
            PayloadItem("chunk-1", INPUT_KIND_RAW, content_fingerprint("Acme Corp.")),
        ]
    )

    single_key = extraction_derivation_key(input_fingerprint=single, config_fingerprint=config)
    duplicated_key = extraction_derivation_key(input_fingerprint=duplicated, config_fingerprint=config)
    reordered_key = extraction_derivation_key(input_fingerprint=reordered, config_fingerprint=config)

    assert single_key != duplicated_key
    assert duplicated_key != reordered_key


def test_extraction_key_changes_on_input_kind_flip_with_identical_text() -> None:
    """The same chunk key and text under raw vs. contextualized input kind is a distinct payload."""
    config = _BASE_CONFIG.config_fingerprint()
    raw_payload = _payload(input_kind=INPUT_KIND_RAW)
    contextualized_payload = _payload(input_kind=INPUT_KIND_CONTEXTUALIZED)

    raw_key = extraction_derivation_key(input_fingerprint=raw_payload, config_fingerprint=config)
    contextualized_key = extraction_derivation_key(input_fingerprint=contextualized_payload, config_fingerprint=config)

    assert raw_key != contextualized_key


def test_extraction_key_is_derivation_key_of_input_and_config() -> None:
    """The key is exactly ``derivation_key(inputs={"input": ..., "config": ...})`` — no upstream key embedded."""
    payload = _payload()
    config = _BASE_CONFIG.config_fingerprint()

    key = extraction_derivation_key(input_fingerprint=payload, config_fingerprint=config)

    assert key == derivation_key(inputs={"input": payload, "config": config})


def test_resolved_extraction_config_rejects_an_unknown_input_policy() -> None:
    """A typo'd ``input_policy`` is rejected at construction, before it can influence any key."""
    with pytest.raises(ValueError, match="input_policy"):
        ResolvedExtractionConfig(extractor_version="v1", materializer_version="1", input_policy="bogus")  # type: ignore[arg-type]


def test_source_occurrence_key_deterministic_for_same_inputs() -> None:
    """Identical ``(chunk_id, span, anchor_text)`` derive an identical occurrence key."""
    kwargs = {
        "chunk_id": "chunk-1",
        "source_span_start": 10,
        "source_span_end": 19,
        "source_anchor_text": "Acme Corp",
    }

    first = derive_source_occurrence_key(**kwargs)
    second = derive_source_occurrence_key(**kwargs)

    assert first == second


def test_source_occurrence_key_changes_when_any_single_input_changes() -> None:
    """Varying any one of the four inputs changes the occurrence key."""
    base_kwargs = {
        "chunk_id": "chunk-1",
        "source_span_start": 10,
        "source_span_end": 19,
        "source_anchor_text": "Acme Corp",
    }
    base = derive_source_occurrence_key(**base_kwargs)

    assert derive_source_occurrence_key(**{**base_kwargs, "chunk_id": "chunk-2"}) != base
    assert derive_source_occurrence_key(**{**base_kwargs, "source_span_start": 11}) != base
    assert derive_source_occurrence_key(**{**base_kwargs, "source_span_end": 20}) != base
    assert derive_source_occurrence_key(**{**base_kwargs, "source_anchor_text": "Acme Corporation"}) != base


def test_source_occurrence_key_is_run_independent() -> None:
    """The occurrence key admits no run-scoped input: its parameters are exactly the occurrence.

    Two extraction runs detecting the same source occurrence derive equal keys
    because the function is a deterministic function of inputs that contain no run
    identity — determinism is asserted by the same-inputs test above; this test
    pins the other half, that the signature is exactly
    ``(chunk_id, source_span_start, source_span_end, source_anchor_text)``, so a
    future edit cannot add a run-scoped parameter without breaking cross-run
    gold-set alignment unnoticed.
    """
    parameters = inspect.signature(derive_source_occurrence_key).parameters

    assert set(parameters) == {"chunk_id", "source_span_start", "source_span_end", "source_anchor_text"}
