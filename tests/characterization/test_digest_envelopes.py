"""Pin the exact hex output of every persisted digest and key primitive.

Each entry in ``fixtures/digests.json`` records the constructor inputs beside the
expected hex, and the builder registry below turns those inputs into a live call.
A change to any algorithm, canonical-JSON envelope, or truncation width fails
here as a value mismatch rather than reaching a persisted row.

The fixture is hand-written and there is no regeneration path: a value is
computed once, pasted, and afterwards only ever edited as a reviewed diff.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

import pytest

from aizk.chunking.datamodel import Chunk, derive_chunk_content_key
from aizk.chunking.splitter import split
from aizk.conversion.core.hashing import compute_config_hash, compute_idempotency_key
from aizk.conversion.core.source_ref import InlineHtmlRef, SingleFileRef, compute_source_ref_hash
from aizk.graph import contextualization as contextualization_module
from aizk.graph.fingerprints import PayloadItem, chunk_key, payload_fingerprint
from aizk.graph.llm import ModelConfig, _default_responder, model_config_fingerprint
from aizk.graph.mention_store import (
    ResolvedExtractionConfig,
    derive_source_occurrence_key,
    extraction_derivation_key,
)
from aizk.graph.persistence import (
    chunking_config_fingerprint,
    chunking_derivation_key,
    chunking_input_fingerprint,
)
from aizk.pipeline.identity import content_fingerprint, derivation_key, fingerprint
from aizk.utilities.hashing import compute_markdown_hash

if TYPE_CHECKING:
    from collections.abc import Callable

_FIXTURE = Path(__file__).parent / "fixtures" / "digests.json"

#: Rows whose only inputs are integers, so no non-ASCII input can be constructed
#: without misrepresenting the parameter's declared type. They carry two integer
#: cases instead; the ``ensure_ascii`` behavior of the shared canonicalization is
#: locked by every other row that feeds it text.
_INTEGER_INPUT_ROWS = frozenset({"aizk.graph.persistence::chunking_config_fingerprint"})

#: Rows computed at import time from module constants, which take no input.
_CONSTANT_ROWS = frozenset(
    {
        "aizk.graph.contextualization::SUMMARY_PROMPT_HASH",
        "aizk.graph.contextualization::CONTEXT_PROMPT_HASH",
    }
)


def _chunk(fields: dict[str, Any]) -> Chunk:
    """Build a splitter chunk from a fixture's field mapping."""
    return Chunk(
        content_hash=fields["content_hash"],
        source_id=fields["source_id"],
        heading_path=tuple(fields["heading_path"]),
        ordinal=fields["ordinal"],
        text=fields["text"],
        char_count=fields["char_count"],
        converted_artifact_id=fields["converted_artifact_id"],
        markdown_hash_xx64=fields["markdown_hash_xx64"],
        span=(fields["span"][0], fields["span"][1]),
        splitter_version=fields["splitter_version"],
    )


def _optional_chunk(fields: dict[str, Any] | None) -> Chunk | None:
    """Build a splitter chunk, or ``None`` for an absent neighbor."""
    return None if fields is None else _chunk(fields)


def _model_config(fields: dict[str, Any]) -> ModelConfig:
    """Build a resolved model identity from a fixture's field mapping."""
    return ModelConfig(
        provider=fields["provider"],
        model=fields["model"],
        settings=tuple((name, value) for name, value in fields.get("settings", [])),
    )


#: Row key (the contract sheet's symbol) → the call that produces the pinned value.
_BUILDERS: dict[str, "Callable[[dict[str, Any]], object]"] = {
    "aizk.utilities.hashing::compute_markdown_hash": lambda i: compute_markdown_hash(i["markdown_text"]),
    "aizk.pipeline.identity::fingerprint": lambda i: fingerprint(i["value"]),
    "aizk.pipeline.identity::content_fingerprint": lambda i: content_fingerprint(i["text"]),
    "aizk.pipeline.identity::derivation_key": lambda i: derivation_key(
        inputs=i["inputs"], upstream_keys=i["upstream_keys"]
    ),
    "aizk.conversion.utilities.hashing::compute_idempotency_key": lambda i: compute_idempotency_key(
        i["source_ref_hash"], i["converter_name"], i["config_snapshot"]
    ),
    "aizk.conversion.utilities.hashing::compute_config_hash": lambda i: compute_config_hash(i["config_payload"]),
    "aizk.conversion.core.source_ref::compute_source_ref_hash": lambda i: compute_source_ref_hash(
        SingleFileRef(url=i["url"])
    ),
    "aizk.conversion.core.source_ref::InlineHtmlRef.to_dedup_payload": lambda i: InlineHtmlRef(
        body=i["body_text"].encode("utf-8")
    ).to_dedup_payload()["content_hash"],
    "aizk.chunking.datamodel::derive_chunk_content_key": lambda i: derive_chunk_content_key(
        i["source_id"], tuple(i["heading_path"]), i["ordinal"], i["content_hash"]
    ),
    "aizk.graph.mention_store::derive_source_occurrence_key": lambda i: derive_source_occurrence_key(
        chunk_id=UUID(i["chunk_id"]),
        source_span_start=i["source_span_start"],
        source_span_end=i["source_span_end"],
        source_anchor_text=i["source_anchor_text"],
    ),
    "aizk.graph.mention_store::ResolvedExtractionConfig.config_fingerprint": lambda i: ResolvedExtractionConfig(
        extractor_version=i["extractor_version"],
        materializer_version=i["materializer_version"],
        input_policy=i["input_policy"],
    ).config_fingerprint(),
    "aizk.graph.mention_store::extraction_derivation_key": lambda i: extraction_derivation_key(
        input_fingerprint=i["input_fingerprint"], config_fingerprint=i["config_fingerprint"]
    ),
    "aizk.graph.fingerprints::payload_fingerprint": lambda i: payload_fingerprint(
        [
            PayloadItem(
                chunk_key=item["chunk_key"],
                input_kind=item["input_kind"],
                text_fingerprint=item["text_fingerprint"],
            )
            for item in i["items"]
        ]
    ),
    "aizk.graph.fingerprints::chunk_key": lambda i: chunk_key(_chunk(i["chunk"])),
    "aizk.graph.contextualization::_stable_hash": lambda i: contextualization_module._stable_hash(i["text"]),
    "aizk.graph.contextualization::_stable_payload_hash": lambda i: contextualization_module._stable_payload_hash(
        i["payload"]
    ),
    "aizk.graph.contextualization::SUMMARY_PROMPT_HASH": lambda i: contextualization_module.SUMMARY_PROMPT_HASH,
    "aizk.graph.contextualization::CONTEXT_PROMPT_HASH": lambda i: contextualization_module.CONTEXT_PROMPT_HASH,
    "aizk.graph.llm::model_config_fingerprint": lambda i: model_config_fingerprint(_model_config(i["model_config"])),
    "aizk.graph.llm::_default_responder": lambda i: _default_responder(i["prompt"]),
    "aizk.graph.persistence::chunking_input_fingerprint": lambda i: chunking_input_fingerprint(
        i["markdown_fingerprint"]
    ),
    "aizk.graph.persistence::chunking_config_fingerprint": lambda i: chunking_config_fingerprint(
        i["splitter_version"]
    ),
    "aizk.graph.persistence::chunking_derivation_key": lambda i: chunking_derivation_key(
        i["markdown_fingerprint"], i["splitter_version"]
    ),
    "aizk.graph.contextualization::summary_input_fingerprint": (
        lambda i: contextualization_module.summary_input_fingerprint(i["document_text"])
    ),
    "aizk.graph.contextualization::summary_config_fingerprint": (
        lambda i: contextualization_module.summary_config_fingerprint(
            _model_config(i["model_config"]), i["summary_version"]
        )
    ),
    "aizk.graph.contextualization::summary_derivation_key": lambda i: contextualization_module.summary_derivation_key(
        i["document_text"], i["summary_version"], _model_config(i["model_config"])
    ),
    "aizk.graph.contextualization::contextualization_run_input_fingerprint": (
        lambda i: contextualization_module.contextualization_run_input_fingerprint(i["summary_text"], i["chunk_keys"])
    ),
    "aizk.graph.contextualization::contextualization_config_fingerprint": (
        lambda i: contextualization_module.contextualization_config_fingerprint(
            i["splitter_version"], i["context_version"], _model_config(i["model_config"])
        )
    ),
    "aizk.graph.contextualization::contextualization_run_derivation_key": (
        lambda i: contextualization_module.contextualization_run_derivation_key(
            i["summary_text"],
            [_chunk(c) for c in i["ordered_chunks"]],
            i["splitter_version"],
            i["context_version"],
            _model_config(i["model_config"]),
        )
    ),
    "aizk.graph.contextualization::contextualization_row_input_fingerprint": (
        lambda i: contextualization_module.contextualization_row_input_fingerprint(
            i["summary_text"],
            _chunk(i["working"]),
            _optional_chunk(i["prior_2"]),
            _optional_chunk(i["prior_1"]),
            _optional_chunk(i["next_1"]),
        )
    ),
    "aizk.graph.contextualization::contextualization_row_derivation_key": (
        lambda i: contextualization_module.contextualization_row_derivation_key(
            i["summary_text"],
            _chunk(i["working"]),
            _optional_chunk(i["prior_2"]),
            _optional_chunk(i["prior_1"]),
            _optional_chunk(i["next_1"]),
            i["splitter_version"],
            i["context_version"],
            _model_config(i["model_config"]),
        )
    ),
    "aizk.chunking.splitter::split": lambda i: [
        c.content_hash
        for c in split(
            i["markdown_text"],
            source_id=i["source_id"],
            converted_artifact_id=i["converted_artifact_id"],
            markdown_hash_xx64=i["markdown_hash_xx64"],
        )
    ],
}


def _load_fixture() -> dict[str, Any]:
    """Read the pinned digest fixture."""
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


_FIXTURE_DATA = _load_fixture()

_CASES = [
    pytest.param(row_key, case, id=f"{row_key}-{case['label']}")
    for row_key, row in sorted(_FIXTURE_DATA.items())
    for case in row["cases"]
]


def test_fixture_covers_every_annotated_digest() -> None:
    """The fixture names exactly the symbols the builder registry knows how to call."""
    assert sorted(_FIXTURE_DATA) == sorted(_BUILDERS)


@pytest.mark.parametrize("row_key", sorted(_FIXTURE_DATA))
def test_each_row_locks_an_ascii_and_a_non_ascii_input(row_key: str) -> None:
    """Every text-taking row pins both an ASCII and a non-ASCII input."""
    labels = [case["label"] for case in _FIXTURE_DATA[row_key]["cases"]]
    assert len(labels) == len(set(labels)), f"{row_key} repeats a case label"
    if row_key in _CONSTANT_ROWS:
        assert labels == ["constant"]
    elif row_key in _INTEGER_INPUT_ROWS:
        assert len(labels) >= 2
    else:
        assert {"ascii", "non_ascii"} <= set(labels)


@pytest.mark.parametrize(("row_key", "case"), _CASES)
def test_digest_matches_pinned_value(row_key: str, case: dict[str, Any]) -> None:
    """The live call over the recorded inputs reproduces the pinned value exactly."""
    assert _BUILDERS[row_key](case["inputs"]) == case["expected"]
