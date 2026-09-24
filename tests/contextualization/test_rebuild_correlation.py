"""A rebuild remints internal identities but preserves correlation through source facts.

Ingests the same `Source` (same `source_ref`/`url`/`title`) and the same
Markdown into two independent databases, standing in for a database rebuild:
each database mints its own `source_id` — no internal identity is required to
survive a rebuild. `pipeline-identity`'s rebuild requirement says correlation
of a logical source across a rebuild is instead achievable through its source
metadata (`source_ref_hash`) and content fingerprints, which stay queryable;
this test asserts exactly that: the two databases' internal identities differ,
while `source_ref_hash` plus the chunking run's raw-content hash
(`ChunkRunInput.markdown_hash_xx64`) and the summary's `summary_text_fingerprint`
agree.

`ChunkRunInput.chunk_set_fingerprint` is deliberately not part of this
comparison: it fingerprints the *emitted chunk set*, keyed per chunk by
`aizk.graph.fingerprints.chunk_key`, which embeds `source_id` to disambiguate
otherwise-identical chunks belonging to different sources — so it changes
whenever `source_id` does, by design, on a rebuild. `markdown_hash_xx64` and
`summary_text_fingerprint` are hashes of raw content alone and carry no
`source_id`, so they are the fingerprints that correlate across a rebuild.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import Engine, text
from sqlmodel import Session, SQLModel, create_engine, select

from aizk.contextualization.processing.document import ProcessResult, process_document
from aizk.conversion.core.source_ref import KarakeepBookmarkRef, compute_source_ref_hash
from aizk.conversion.datamodel.source import Source
from aizk.graph.chunks import CHUNKING_STAGE
from aizk.graph.content_index import CONTENT_FTS_DDL
from aizk.graph.datamodel import ChunkRunInput, DocumentSummary
from aizk.graph.llm import STUB_MODEL_CONFIG, StubLLMClient
from aizk.graph.variants import SUMMARY_STAGE
from aizk.pipeline.run import PipelineRun, RunStatus
from tests.fakes.markdown import StubMarkdownSource

_KARAKEEP_ID = "bm-rebuild-fixture"
_TITLE = "Rebuild Fixture"
_URL = "https://example.com/rebuild-fixture"

_MARKDOWN = """# Title

A paragraph under the title with enough text to be a real chunk.

## Section One

Content for the first section that the splitter carves into a chunk.
"""


class _AlwaysCurrent:
    """A freshness stub treating every output as the source's latest."""

    def is_current(self, session: Session, source_id: UUID, conversion_output_id: int) -> bool:
        """Always report the output as current."""
        return True


def _make_engine(tmp_path: Path, name: str) -> Engine:
    """Create a file-based SQLite engine with the full registered schema."""
    engine = create_engine(f"sqlite:///{tmp_path / name}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text(CONTENT_FTS_DDL))
    return engine


def _seed_and_ingest(engine: Engine, source_id: UUID) -> None:
    """Insert the fixture `Source` row and run the write path for it."""
    ref = KarakeepBookmarkRef(bookmark_id=_KARAKEEP_ID)
    with Session(engine) as session:
        session.add(
            Source(
                source_id=source_id,
                karakeep_id=_KARAKEEP_ID,
                source_ref=ref.model_dump_json(),
                source_ref_hash=compute_source_ref_hash(ref),
                owner_id="self",
                url=_URL,
                normalized_url=_URL,
                title=_TITLE,
            )
        )
        session.commit()

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


def _correlation_facts(engine: Engine, source_id: UUID) -> tuple[str, str, str]:
    """Return `(source_ref_hash, markdown_hash_xx64, summary_text_fingerprint)` for `source_id`."""
    with Session(engine) as session:
        source = session.exec(select(Source).where(Source.source_id == source_id)).one()
        chunking_run = session.exec(
            select(PipelineRun).where(
                PipelineRun.stage == CHUNKING_STAGE,
                PipelineRun.scope_id == source_id,
                PipelineRun.status == RunStatus.ACTIVE,
            )
        ).one()
        summary_run = session.exec(
            select(PipelineRun).where(
                PipelineRun.stage == SUMMARY_STAGE,
                PipelineRun.scope_id == source_id,
                PipelineRun.status == RunStatus.ACTIVE,
            )
        ).one()
        chunk_input = session.exec(select(ChunkRunInput).where(ChunkRunInput.run_id == chunking_run.id)).one()
        summary = session.exec(select(DocumentSummary).where(DocumentSummary.run_id == summary_run.id)).one()
        return source.source_ref_hash, chunk_input.markdown_hash_xx64, summary.summary_text_fingerprint


def test_a_rebuild_remints_identities_but_preserves_correlation(tmp_path: Path) -> None:
    """Two independent databases ingesting the same fixture correlate by source facts, not by internal identity."""
    engine_a = _make_engine(tmp_path, "a.db")
    engine_b = _make_engine(tmp_path, "b.db")
    source_id_a = uuid4()
    source_id_b = uuid4()

    _seed_and_ingest(engine_a, source_id_a)
    _seed_and_ingest(engine_b, source_id_b)

    assert source_id_a != source_id_b, "each database mints its own internal source identity"

    facts_a = _correlation_facts(engine_a, source_id_a)
    facts_b = _correlation_facts(engine_b, source_id_b)
    assert facts_a == facts_b, (
        "the same logical source must be matchable across the two databases by source_ref_hash plus the "
        "chunking run's raw-content hash and the summary's content fingerprint, despite differing internal identities"
    )
