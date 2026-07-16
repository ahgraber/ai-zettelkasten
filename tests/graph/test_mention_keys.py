"""Mention-store key-derivation tests.

Requirement: an extraction run's derivation key and a source-anchored mention's
``source_occurrence_key`` are deterministic functions of exactly their declared
inputs, changing when any single input changes, with no local surrogate id (a
``run_id`` or row id) admitted into either. These guard the two portability
properties the mention store's idempotency and cross-run diffing rely on:
recomputing a key from the same inputs on any process or backend yields the same
value, and the ``source_occurrence_key`` needs no run to be computed.
"""

from __future__ import annotations

import inspect
import json

from aizk.graph.mention_store import derive_source_occurrence_key, extraction_derivation_key


def test_extraction_key_deterministic_for_same_inputs() -> None:
    """Identical semantic inputs derive an identical extraction derivation key."""
    kwargs = {
        "extractor_version": "spacy-en_core_web_sm-3.7-v1",
        "materializer_version": "1",
        "input_policy": "contextualized",
        "upstream_derivation_key": '{"context_version":1}',
    }

    first = extraction_derivation_key(**kwargs)
    second = extraction_derivation_key(**kwargs)

    assert first == second


def test_extraction_key_changes_when_any_single_input_changes() -> None:
    """Varying any one of the four inputs changes the extraction derivation key."""
    base_kwargs = {
        "extractor_version": "spacy-en_core_web_sm-3.7-v1",
        "materializer_version": "1",
        "input_policy": "raw",
        "upstream_derivation_key": '{"markdown_hash":"abc","splitter_version":1}',
    }
    base = extraction_derivation_key(**base_kwargs)

    assert extraction_derivation_key(**{**base_kwargs, "extractor_version": "gliner2-v1"}) != base
    assert extraction_derivation_key(**{**base_kwargs, "materializer_version": "2"}) != base
    assert extraction_derivation_key(**{**base_kwargs, "input_policy": "contextualized"}) != base
    assert extraction_derivation_key(**{**base_kwargs, "upstream_derivation_key": "different"}) != base


def test_extraction_key_embeds_no_local_surrogate_id() -> None:
    """The key is exactly the four semantic fields — no run id, row id, or other local surrogate.

    The function's signature admits only the four semantic inputs to begin with;
    this also asserts the serialized key's field set is exactly those four names,
    so a future edit cannot silently smuggle a local id into the encoding without
    the assertion catching it.
    """
    key = extraction_derivation_key(
        extractor_version="spacy-en_core_web_sm-3.7-v1",
        materializer_version="1",
        input_policy="raw",
        upstream_derivation_key="upstream-key",
    )

    decoded = json.loads(key)
    assert set(decoded) == {"extractor_version", "materializer_version", "input_policy", "upstream_derivation_key"}


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
