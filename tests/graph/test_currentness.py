"""Tests for monotonic currentness (High-1) and lock-free generation (High-2).

- A late older conversion output cannot supersede a newer one's runs: the
  freshness gate skips it, leaving the newer generation active.
- The summary/revision LLM passes run *before* the persist transaction opens, so
  model latency never holds the single serialized SQLite write lock.

Both use the real :class:`~aizk.graph.markdown_source.S3MarkdownSource` /
:class:`~aizk.graph.markdown_source.ConversionOutputFreshness` over a fake blob
reader and ``conversion_outputs`` rows (FK enforcement off, so standalone rows
suffice).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from sqlmodel import Session, SQLModel, create_engine, select

from aizk.conversion.datamodel.output import ConversionOutput
from aizk.graph.llm import StubLLMClient
from aizk.graph.markdown_source import ConversionOutputFreshness, S3MarkdownSource
from aizk.graph.persistence import run_input
from aizk.graph.workunit import LoadedMarkdown, ProcessResult, SkippedSuperseded, process_document
from aizk.pipeline.run import PipelineRun, RunStatus
from aizk.utilities.hashing import compute_markdown_hash

_AIZK_UUID = UUID("11111111-1111-1111-1111-111111111111")

_MARKDOWN_OLD = "# Old\n\nThe first conversion's body text, long enough to chunk.\n"
_MARKDOWN_NEW = "# New\n\nThe second conversion's body text, also long enough to chunk.\n"


class _FakeBlobReader:
    def __init__(self, blobs: dict[str, bytes]) -> None:
        self._blobs = blobs

    def get_object_bytes(self, s3_key: str) -> bytes:
        return self._blobs[s3_key]


def _make_engine(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'currentness.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return engine


def _add_output(session: Session, *, output_id: int, markdown: str, aizk_uuid: UUID = _AIZK_UUID) -> None:
    session.add(
        ConversionOutput(
            id=output_id,
            job_id=output_id,
            aizk_uuid=aizk_uuid,
            owner_id="owner",
            title="Doc",
            payload_version=1,
            s3_prefix=f"prefix-{output_id}",
            markdown_key=f"prefix-{output_id}/output.md",
            manifest_key=f"prefix-{output_id}/manifest.json",
            markdown_hash_xx64=compute_markdown_hash(markdown),
            docling_version="1.0",
            pipeline_name="docling",
        )
    )


def _active_chunking_input(engine, aizk_uuid: UUID) -> str:
    """Return the conversion_output_id recorded by the source's active chunking run."""
    with Session(engine) as session:
        run = session.exec(
            select(PipelineRun).where(
                PipelineRun.stage == "chunking",
                PipelineRun.scope_key == str(aizk_uuid),
                PipelineRun.status == RunStatus.ACTIVE,
            )
        ).one()
        return run_input(session, run.id).conversion_output_id


def test_stale_output_cannot_supersede_newer(tmp_path: Path) -> None:
    """After the newer output's runs are active, a late older output is skipped, not superseding."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        _add_output(session, output_id=1, markdown=_MARKDOWN_OLD)
        _add_output(session, output_id=2, markdown=_MARKDOWN_NEW)
        session.commit()

    markdown_source = S3MarkdownSource(
        engine,
        _FakeBlobReader(
            {"prefix-1/output.md": _MARKDOWN_OLD.encode("utf-8"), "prefix-2/output.md": _MARKDOWN_NEW.encode("utf-8")}
        ),
    )
    freshness = ConversionOutputFreshness()

    # The newer output (id 2) wins and establishes the active runs.
    newer = process_document(
        engine,
        StubLLMClient(),
        aizk_uuid=_AIZK_UUID,
        conversion_output_id=2,
        markdown_source=markdown_source,
        freshness=freshness,
    )
    assert isinstance(newer, ProcessResult)
    assert _active_chunking_input(engine, _AIZK_UUID) == "2"

    # The older output (id 1) runs late — it must not supersede the newer runs.
    older = process_document(
        engine,
        StubLLMClient(),
        aizk_uuid=_AIZK_UUID,
        conversion_output_id=1,
        markdown_source=markdown_source,
        freshness=freshness,
    )
    assert isinstance(older, SkippedSuperseded)
    assert older.conversion_output_id == 1
    # The active chunking generation is still the newer output's; nothing superseded.
    assert _active_chunking_input(engine, _AIZK_UUID) == "2"
    with Session(engine) as session:
        active_chunking = session.exec(
            select(PipelineRun).where(
                PipelineRun.stage == "chunking",
                PipelineRun.scope_key == str(_AIZK_UUID),
                PipelineRun.status == RunStatus.ACTIVE,
            )
        ).all()
        assert len(active_chunking) == 1
        superseded = session.exec(
            select(PipelineRun).where(
                PipelineRun.stage == "chunking",
                PipelineRun.scope_key == str(_AIZK_UUID),
                PipelineRun.status == RunStatus.SUPERSEDED,
            )
        ).all()
        assert superseded == []


class _RecordingSource:
    """A Markdown source that records each ``load`` so a test can assert it was not fetched."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.loads: list[int] = []

    def load(self, conversion_output_id: int) -> LoadedMarkdown:
        self.loads.append(conversion_output_id)
        return LoadedMarkdown(text=self.text, markdown_hash_xx64=compute_markdown_hash(self.text))


def test_superseded_output_is_skipped_before_fetch_and_generation(tmp_path: Path) -> None:
    """A superseded output is skipped by the read-only preflight, before any fetch or model call.

    The early ownership/currentness preflight returns ``SkippedSuperseded`` before
    the Markdown blob is fetched or the LLM runs, so stale/foreign content never
    reaches the model and no work is wasted — while the authoritative re-check still
    runs inside the persist transaction for the units that do proceed.
    """
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        _add_output(session, output_id=1, markdown=_MARKDOWN_OLD)
        _add_output(session, output_id=2, markdown=_MARKDOWN_NEW)
        session.commit()

    source = _RecordingSource(_MARKDOWN_OLD)
    client = StubLLMClient()

    result = process_document(
        engine,
        client,
        aizk_uuid=_AIZK_UUID,
        conversion_output_id=1,  # already superseded by output 2
        markdown_source=source,
        freshness=ConversionOutputFreshness(),
    )

    assert isinstance(result, SkippedSuperseded)
    assert source.loads == [], "the preflight must skip before fetching the Markdown blob"
    assert client.prompts == [], "the preflight must skip before any model call"


@dataclass
class _InMemorySource:
    text: str

    def load(self, conversion_output_id: int) -> LoadedMarkdown:
        return LoadedMarkdown(text=self.text, markdown_hash_xx64=compute_markdown_hash(self.text))


class _RecordingFreshness:
    """Records how many model calls had been made by the time the persist gate runs."""

    def __init__(self, client: StubLLMClient) -> None:
        self._client = client
        self.prompts_at_gate: int | None = None

    def is_current(self, session: Session, aizk_uuid: UUID, conversion_output_id: int) -> bool:
        self.prompts_at_gate = len(self._client.prompts)
        return True


def test_generation_runs_before_the_write_transaction(tmp_path: Path) -> None:
    """The summary/revision LLM passes complete before the persist transaction's gate.

    The freshness gate is the first thing inside the ``BEGIN IMMEDIATE`` persist
    transaction; if model calls have already happened by then, generation ran with
    no write lock held (High-2).
    """
    engine = _make_engine(tmp_path)
    client = StubLLMClient()
    freshness = _RecordingFreshness(client)

    result = process_document(
        engine,
        client,
        aizk_uuid=_AIZK_UUID,
        conversion_output_id=1,
        markdown_source=_InMemorySource(_MARKDOWN_NEW),
        freshness=freshness,
    )

    assert isinstance(result, ProcessResult)
    # The summary pass (at least) ran before the write transaction's freshness gate.
    assert freshness.prompts_at_gate is not None and freshness.prompts_at_gate >= 1


def test_output_from_another_source_is_not_persisted_under_the_wrong_source(tmp_path: Path) -> None:
    """A (conversion_output_id, aizk_uuid) mismatch is skipped, not written under the wrong source.

    Source B's output has a higher id than source A's latest; without an identity
    check the freshness gate would accept it for A (id >= A's max), persisting B's
    Markdown under A. The gate now verifies the output row belongs to the source.
    """
    source_b = UUID("22222222-2222-2222-2222-222222222222")
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        _add_output(session, output_id=1, markdown=_MARKDOWN_OLD, aizk_uuid=_AIZK_UUID)
        _add_output(session, output_id=2, markdown=_MARKDOWN_NEW, aizk_uuid=source_b)
        session.commit()

    markdown_source = S3MarkdownSource(
        engine,
        _FakeBlobReader(
            {"prefix-1/output.md": _MARKDOWN_OLD.encode("utf-8"), "prefix-2/output.md": _MARKDOWN_NEW.encode("utf-8")}
        ),
    )

    # Process source A but point at source B's output id (2 > A's max id 1).
    result = process_document(
        engine,
        StubLLMClient(),
        aizk_uuid=_AIZK_UUID,
        conversion_output_id=2,
        markdown_source=markdown_source,
        freshness=ConversionOutputFreshness(),
    )

    assert isinstance(result, SkippedSuperseded)
    with Session(engine) as session:
        runs_under_a = session.exec(select(PipelineRun).where(PipelineRun.scope_key == str(_AIZK_UUID))).all()
        assert runs_under_a == [], "no runs may be written under source A from source B's output"
