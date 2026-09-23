"""Portability test: identical semantic inputs across two databases compute equal keys.

Ingests the same Markdown fixture, under the same `source_id`, into two
independent SQLite databases through `process_document` (chunking + summary +
contextualization) and `extract_document` (extraction), with the same stub LLM
client and a silent extractor. `source_id` is the semantic identity a
derivation key is legitimately built from (it disambiguates two different
sources' otherwise-identical chunks — see `aizk.graph.fingerprints.chunk_key`),
so it is held equal across the two databases here; only the database-assigned
surrogates (`chunk_id`, `pipeline_runs.id`) are minted independently per engine.
Every run-producing stage's `derivation_key` comes out equal across the two
databases regardless — the portability invariant
(`aizk.pipeline.identity.derivation_key`) holds all the way through the stack,
not just at the shared helper.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import Engine, text
from sqlmodel import Session, SQLModel, create_engine, select

from aizk.graph.content_index import CONTENT_FTS_DDL
from aizk.graph.contextualization import SUMMARY_STAGE, VARIANT_STAGE
from aizk.graph.datamodel import Chunk
from aizk.graph.extraction_run import extract_document
from aizk.graph.llm import STUB_MODEL_CONFIG, StubLLMClient
from aizk.graph.mention_store import INPUT_POLICY_RAW, MENTION_EXTRACTION_STAGE
from aizk.graph.persistence import CHUNKING_STAGE
from aizk.graph.workunit import ProcessResult, process_document
from aizk.pipeline.run import PipelineRun, RunStatus
from tests.fakes.markdown import StubMarkdownSource

_SOURCE_ID = UUID("11111111-1111-1111-1111-111111111111")

_MARKDOWN = """# Title

A paragraph under the title with enough text to be a real chunk.

## Section One

Content for the first section that the splitter carves into a chunk.

## Section Two

Content for the second section, distinct from the first.
"""


class _AlwaysCurrent:
    """A freshness stub treating every output as the source's latest."""

    def is_current(self, session: Session, source_id: UUID, conversion_output_id: int) -> bool:
        """Always report the output as current."""
        return True


class _SilentExtractor:
    """A deterministic `EntityExtractor` test double that detects nothing."""

    def __init__(self) -> None:
        self.extractor_version = "stub/v1"

    def extract(self, text_: str) -> list:
        """Return no detections for any input text."""
        return []


def _make_engine(tmp_path: Path, name: str) -> Engine:
    """Create a file-based SQLite engine with the full registered schema."""
    engine = create_engine(f"sqlite:///{tmp_path / name}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text(CONTENT_FTS_DDL))
    return engine


def _ingest(engine: Engine, source_id: UUID) -> None:
    """Run the full write path (chunking + summary + contextualization + extraction) for one source."""
    result = process_document(
        engine,
        StubLLMClient(),
        source_id=source_id,
        conversion_output_id=1,
        markdown_source=StubMarkdownSource(_MARKDOWN),
        freshness=_AlwaysCurrent(),
        model_config=STUB_MODEL_CONFIG,
    )
    assert isinstance(result, ProcessResult)
    extract_document(engine, source_id=source_id, extractor=_SilentExtractor(), input_policy=INPUT_POLICY_RAW)


def _active_derivation_keys(engine: Engine, source_id: UUID) -> dict[str, str]:
    """Return each stage's active run derivation key for `source_id`, keyed by stage name."""
    with Session(engine) as session:
        runs = session.exec(
            select(PipelineRun).where(PipelineRun.scope_id == source_id, PipelineRun.status == RunStatus.ACTIVE)
        ).all()
    return {run.stage: run.derivation_key for run in runs}


def _chunk_ids(engine: Engine, source_id: UUID) -> set[UUID]:
    """Return the set of persisted chunk surrogates for `source_id`."""
    with Session(engine) as session:
        return set(session.exec(select(Chunk.chunk_id).where(Chunk.source_id == source_id)).all())


def test_identical_inputs_across_two_databases_yield_equal_derivation_keys(tmp_path: Path) -> None:
    """The same document, under the same source, ingested into two independent databases computes equal keys.

    Each database mints its own chunk surrogates independently (asserted
    below); no database-assigned surrogate enters a derivation key, so the
    keys themselves — a function of semantic content alone — come out equal
    across the two databases.
    """
    engine_a = _make_engine(tmp_path, "a.db")
    engine_b = _make_engine(tmp_path, "b.db")

    _ingest(engine_a, _SOURCE_ID)
    _ingest(engine_b, _SOURCE_ID)

    chunk_ids_a = _chunk_ids(engine_a, _SOURCE_ID)
    chunk_ids_b = _chunk_ids(engine_b, _SOURCE_ID)
    assert chunk_ids_a, "the fixture must produce at least one chunk"
    assert chunk_ids_a.isdisjoint(chunk_ids_b), "each database mints its own chunk surrogates independently"

    keys_a = _active_derivation_keys(engine_a, _SOURCE_ID)
    keys_b = _active_derivation_keys(engine_b, _SOURCE_ID)

    assert keys_a.keys() == {CHUNKING_STAGE, SUMMARY_STAGE, VARIANT_STAGE, MENTION_EXTRACTION_STAGE}
    assert keys_a == keys_b, "identical semantic inputs must compute identical derivation keys per stage"
