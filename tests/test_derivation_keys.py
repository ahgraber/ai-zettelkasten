"""Cross-stage tests for the output-sensitive key composition.

Every shipped derivation-key constructor — chunking, summary, contextualization
run, contextualization row, extraction — composes through the same shared
helper, :func:`aizk.pipeline.identity.derivation_key`, over an ``"input"`` and a
``"config"`` fingerprint. This module pins that composition per stage, pins that
every run-producing stage stamps ``derivation_key_format`` alongside the two
fingerprints on its ``version_stamps``, and guards that no production code
structurally decodes a derivation key (it is an opaque digest; production code
compares two keys for equality and never parses one).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import Engine, text
from sqlmodel import Session, SQLModel, create_engine, select
import xxhash

from aizk.chunking import Chunk as SplitterChunk
from aizk.graph.content_index import CONTENT_FTS_DDL
from aizk.graph.contextualization import (
    SUMMARY_STAGE,
    VARIANT_STAGE,
    contextualization_config_fingerprint,
    contextualization_row_derivation_key,
    contextualization_row_input_fingerprint,
    contextualization_run_derivation_key,
    contextualization_run_input_fingerprint,
    summary_config_fingerprint,
    summary_derivation_key,
    summary_input_fingerprint,
)
from aizk.graph.extraction_run import extract_document
from aizk.graph.fingerprints import chunk_key
from aizk.graph.llm import STUB_MODEL_CONFIG, StubLLMClient
from aizk.graph.mention_store import INPUT_POLICY_RAW, MENTION_EXTRACTION_STAGE, extraction_derivation_key
from aizk.graph.persistence import (
    CHUNKING_STAGE,
    chunking_config_fingerprint,
    chunking_derivation_key,
    chunking_input_fingerprint,
)
from aizk.graph.workunit import LoadedMarkdown, ProcessResult, process_document
from aizk.pipeline.identity import DERIVATION_KEY_FORMAT, content_fingerprint, derivation_key
from aizk.pipeline.run import PipelineRun, RunStatus
from aizk.utilities.hashing import compute_markdown_hash

if TYPE_CHECKING:
    from collections.abc import Sequence

    from aizk.graph.extraction import Detection

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ROOT = _REPO_ROOT / "src" / "aizk"

_SOURCE_ID = UUID("11111111-1111-1111-1111-111111111111")

_MARKDOWN = """# Title

A paragraph under the title with enough text to be a real chunk.

## Section One

Content for the first section that the splitter carves into a chunk.
"""


def _is_sha256_hex(value: str) -> bool:
    """Return whether ``value`` is a 64-character lowercase hex digest."""
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _make_chunk(text_: str, *, ordinal: int) -> SplitterChunk:
    """Build a minimal splitter chunk for the pure key-constructor tests."""
    return SplitterChunk(
        content_hash=xxhash.xxh64(text_.encode("utf-8")).hexdigest(),
        source_id=str(_SOURCE_ID),
        heading_path=(),
        ordinal=ordinal,
        text=text_,
        char_count=len(text_),
        converted_artifact_id="output-1",
        markdown_hash_xx64="deadbeefdeadbeef",
        span=(0, len(text_)),
        splitter_version=3,
    )


def test_chunking_derivation_key_matches_the_shared_helper() -> None:
    """`chunking_derivation_key` is `derivation_key({"input": ..., "config": ...})` over its own fingerprints."""
    markdown_fingerprint = content_fingerprint(_MARKDOWN)
    splitter_version = 3
    key = chunking_derivation_key(markdown_fingerprint, splitter_version)
    assert key == derivation_key(
        inputs={
            "input": chunking_input_fingerprint(markdown_fingerprint),
            "config": chunking_config_fingerprint(splitter_version),
        }
    )
    assert _is_sha256_hex(key)


def test_summary_derivation_key_matches_the_shared_helper() -> None:
    """`summary_derivation_key` is `derivation_key({"input": ..., "config": ...})` over its own fingerprints."""
    summary_version = 2
    key = summary_derivation_key(_MARKDOWN, summary_version, STUB_MODEL_CONFIG)
    assert key == derivation_key(
        inputs={
            "input": summary_input_fingerprint(_MARKDOWN),
            "config": summary_config_fingerprint(STUB_MODEL_CONFIG, summary_version),
        }
    )
    assert _is_sha256_hex(key)


def test_contextualization_run_derivation_key_matches_the_shared_helper() -> None:
    """`contextualization_run_derivation_key` is `derivation_key(...)` over its own fingerprints."""
    summary_text = "A one-paragraph document summary."
    chunks = [_make_chunk("first chunk body", ordinal=0), _make_chunk("second chunk body", ordinal=1)]
    splitter_version, context_version = 3, 1
    key = contextualization_run_derivation_key(
        summary_text, chunks, splitter_version, context_version, STUB_MODEL_CONFIG
    )
    assert key == derivation_key(
        inputs={
            "input": contextualization_run_input_fingerprint(summary_text, [chunk_key(c) for c in chunks]),
            "config": contextualization_config_fingerprint(splitter_version, context_version, STUB_MODEL_CONFIG),
        }
    )
    assert _is_sha256_hex(key)


def test_contextualization_row_derivation_key_matches_the_shared_helper() -> None:
    """`contextualization_row_derivation_key` is `derivation_key(...)` over its own fingerprints."""
    summary_text = "A one-paragraph document summary."
    working = _make_chunk("working chunk body", ordinal=2)
    prior_1 = _make_chunk("prior chunk body", ordinal=1)
    splitter_version, context_version = 3, 1
    key = contextualization_row_derivation_key(
        summary_text, working, None, prior_1, None, splitter_version, context_version, STUB_MODEL_CONFIG
    )
    assert key == derivation_key(
        inputs={
            "input": contextualization_row_input_fingerprint(summary_text, working, None, prior_1, None),
            "config": contextualization_config_fingerprint(splitter_version, context_version, STUB_MODEL_CONFIG),
        }
    )
    assert _is_sha256_hex(key)


def test_extraction_derivation_key_matches_the_shared_helper() -> None:
    """`extraction_derivation_key` is `derivation_key({"input": ..., "config": ...})` verbatim."""
    input_fingerprint = content_fingerprint("payload")
    config_fingerprint = content_fingerprint("config")
    key = extraction_derivation_key(input_fingerprint=input_fingerprint, config_fingerprint=config_fingerprint)
    assert key == derivation_key(inputs={"input": input_fingerprint, "config": config_fingerprint})
    assert _is_sha256_hex(key)


class _StubMarkdownSource:
    """A deterministic markdown source returning fixed text for any locator."""

    def __init__(self, text_: str) -> None:
        self._text = text_

    def load(self, conversion_output_id: int) -> LoadedMarkdown:
        """Return the fixed Markdown and its computed hash for any locator."""
        return LoadedMarkdown(text=self._text, markdown_hash_xx64=compute_markdown_hash(self._text))


class _AlwaysCurrent:
    """A freshness stub treating every output as the source's latest."""

    def is_current(self, session: Session, source_id: UUID, conversion_output_id: int) -> bool:
        """Always report the output as current."""
        return True


class _SilentExtractor:
    """A deterministic `EntityExtractor` test double that detects nothing."""

    def __init__(self) -> None:
        self.extractor_version = "stub/v1"

    def extract(self, text_: str) -> "Sequence[Detection]":
        """Return no detections for any input text."""
        return []


def _make_engine(tmp_path: Path, name: str) -> Engine:
    """Create a file-based SQLite engine with the full registered schema."""
    engine = create_engine(f"sqlite:///{tmp_path / name}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text(CONTENT_FTS_DDL))
    return engine


def test_every_stage_producer_stamps_the_derivation_key_format(tmp_path: Path) -> None:
    """Chunking, summary, contextualization, and extraction all stamp the shared key-format marker.

    Drives a tiny fixture through `process_document` (chunking + summary +
    contextualization) with the stub LLM client and `extract_document`
    (extraction) with a silent extractor, then asserts every active run's
    `version_stamps_json` carries `derivation_key_format`, `input_fingerprint`,
    and `config_fingerprint`.
    """
    engine = _make_engine(tmp_path, "stamps.db")
    source_id = uuid4()
    result = process_document(
        engine,
        StubLLMClient(),
        source_id=source_id,
        conversion_output_id=1,
        markdown_source=_StubMarkdownSource(_MARKDOWN),
        freshness=_AlwaysCurrent(),
        model_config=STUB_MODEL_CONFIG,
    )
    assert isinstance(result, ProcessResult)
    extract_document(engine, source_id=source_id, extractor=_SilentExtractor(), input_policy=INPUT_POLICY_RAW)

    with Session(engine) as session:
        active_runs = session.exec(
            select(PipelineRun).where(PipelineRun.scope_id == source_id, PipelineRun.status == RunStatus.ACTIVE)
        ).all()

    stages = {run.stage for run in active_runs}
    assert stages == {CHUNKING_STAGE, SUMMARY_STAGE, VARIANT_STAGE, MENTION_EXTRACTION_STAGE}
    for run in active_runs:
        stamps = json.loads(run.version_stamps_json)
        assert stamps["derivation_key_format"] == DERIVATION_KEY_FORMAT, run.stage
        assert "input_fingerprint" in stamps, run.stage
        assert "config_fingerprint" in stamps, run.stage


def _json_loads_calls(tree: ast.AST):
    """Yield every `json.loads(...)` call node in `tree`."""
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "loads"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "json"
        ):
            yield node


def _mentions_derivation_key(node: ast.AST) -> bool:
    """Return whether `node`'s unparsed source text mentions `derivation_key`."""
    return "derivation_key" in ast.unparse(node)


def _assigned_from_derivation_key(tree: ast.AST, name: str) -> bool:
    """Return whether `name` is ever assigned an expression mentioning `derivation_key`."""
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
            and _mentions_derivation_key(node.value)
        ):
            return True
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
            and node.value is not None
            and _mentions_derivation_key(node.value)
        ):
            return True
    return False


def test_no_structural_read_of_a_derivation_key() -> None:
    """No production code parses a derivation key's structure via `json.loads`.

    A derivation key is an opaque SHA-256 digest — production code compares two
    keys for equality and never decodes one. Flags a `json.loads` call whose
    argument mentions `derivation_key` directly, or whose argument is a local
    variable assigned from an expression that does. A `version_stamps_json`
    read (the human-readable stamps recorded alongside a key) does not mention
    `derivation_key` in its own name and is not flagged.
    """
    offenders: list[str] = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for call in _json_loads_calls(tree):
            if not call.args:
                continue
            arg = call.args[0]
            flagged = _mentions_derivation_key(arg) or (
                isinstance(arg, ast.Name) and _assigned_from_derivation_key(tree, arg.id)
            )
            if flagged:
                offenders.append(f"{path.relative_to(_REPO_ROOT)}:{call.lineno}: {ast.unparse(call)}")
    assert offenders == [], "found a structural read of a derivation key:\n" + "\n".join(offenders)


def test_guard_scans_at_least_one_file() -> None:
    """Sanity check: the guard actually walks source files (a silently-empty scan would be a false pass)."""
    assert any(_SRC_ROOT.rglob("*.py")), "expected src/aizk to contain Python files"
