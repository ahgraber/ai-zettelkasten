"""Shared test factories for conversion-service tests.

These helpers replace per-file `_create_bookmark` / `_create_job` copies. They persist
rows through the passed SQLModel session and return the refreshed ORM object.
"""

from __future__ import annotations

from collections.abc import Callable
import datetime as dt
import json
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from aizk.conversion.core.source_ref import KarakeepBookmarkRef, compute_source_ref_hash
from aizk.conversion.datamodel.job import ConversionJob
from aizk.conversion.datamodel.source import Source
from aizk.pipeline.lifecycle import WorkUnitStatus

if TYPE_CHECKING:
    import pytest
    from sqlalchemy.engine import Engine
    from sqlmodel import Session

    from aizk.conversion.settings import ConversionConfig
    from aizk.pipeline.runner import StageRunner


def make_source(
    session: "Session",
    karakeep_id: str,
    *,
    source_id: UUID | None = None,
    source_ref_bookmark_id: str | None = None,
    url: str | None = None,
    title: str | None = None,
    content_type: str | None = None,
    source_type: str | None = None,
    owner_id: str = "self",
) -> Source:
    """Create, persist, and return a Source row keyed by `karakeep_id`.

    `source_ref_bookmark_id` lets callers keep the `karakeep_id` column distinct from the
    ref's `bookmark_id` field (used by dot-containing ID edge cases in test_output_content).
    `owner_id` defaults to the shipped `AIZK_DEFAULT_PRINCIPAL` so the column's NOT NULL
    constraint is satisfied without forcing every test to pass it explicitly.
    """
    ref = KarakeepBookmarkRef(bookmark_id=source_ref_bookmark_id or karakeep_id)
    kwargs = {
        "karakeep_id": karakeep_id,
        "source_ref": ref.model_dump_json(),
        "source_ref_hash": compute_source_ref_hash(ref),
        "owner_id": owner_id,
        "url": url,
        "normalized_url": url,
        "title": title,
        "content_type": content_type,
        "source_type": source_type,
    }
    if source_id is not None:
        kwargs["source_id"] = source_id
    source = Source(**kwargs)
    session.add(source)
    session.commit()
    session.refresh(source)
    return source


def make_job(
    session: "Session",
    *,
    source_id: UUID,
    idempotency_key: str,
    status: WorkUnitStatus = WorkUnitStatus.QUEUED,
    title: str = "Test",
    attempts: int = 0,
    created_at: dt.datetime | None = None,
    owner_id: str = "self",
    earliest_next_attempt_at: dt.datetime | None = None,
    phase: str | None = None,
) -> ConversionJob:
    """Create, persist, and return a ConversionJob row.

    `owner_id` defaults to the shipped `AIZK_DEFAULT_PRINCIPAL` so the column's
    NOT NULL constraint is satisfied without forcing every test to pass it.
    """
    job = ConversionJob(
        source_id=source_id,
        owner_id=owner_id,
        title=title,
        payload_version=1,
        status=status,
        attempts=attempts,
        idempotency_key=idempotency_key,
        earliest_next_attempt_at=earliest_next_attempt_at,
        phase=phase,
    )
    if created_at is not None:
        job.created_at = created_at
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def make_queued_conversion(session: "Session", karakeep_id: str) -> ConversionJob:
    """Create a Source and a ``queued`` job carrying its ``source_ref``, and return the job."""
    source = make_source(session, karakeep_id)
    job = make_job(session, source_id=source.source_id, idempotency_key=uuid4().hex)
    job.source_ref = source.source_ref
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def convert_inline(monkeypatch: "pytest.MonkeyPatch", *, on_convert: Callable[[int], None] | None = None) -> list[int]:
    """Run the conversion subprocess inline and return the ids of the jobs it converts, in order.

    Each conversion calls ``on_convert`` with the job id, reports the
    ``converting`` phase, then writes a one-line markdown document and its
    metadata into the workspace.
    """
    from aizk.conversion.storage.paths import OUTPUT_MARKDOWN_FILENAME, metadata_path
    from aizk.conversion.wiring import subprocess as wiring_subprocess
    from aizk.utilities.hashing import compute_markdown_hash
    from tests.fakes.process import InlineContext

    converted: list[int] = []

    def _convert(job_id: int, workspace_path: str, _source_ref_json: str, status_queue) -> None:
        converted.append(job_id)
        if on_convert is not None:
            on_convert(job_id)
        status_queue.put_nowait({"event": "phase", "message": "converting"})
        workspace = Path(workspace_path)
        markdown = "# Converted\n"
        (workspace / OUTPUT_MARKDOWN_FILENAME).write_text(markdown)
        metadata = {
            "pipeline_name": "html",
            "fetched_at": "2026-01-01T00:00:00+00:00",
            "markdown_filename": OUTPUT_MARKDOWN_FILENAME,
            "figure_files": [],
            "markdown_hash_xx64": compute_markdown_hash(markdown),
            "docling_version": "test",
            "config_snapshot": {"converter_name": "docling"},
            "terminal_ref": {"kind": "karakeep_bookmark", "bookmark_id": "bm_inline"},
            "content_type": "html",
            "source_meta": {},
            "document_title": None,
            "source_title": None,
        }
        metadata_path(workspace).write_text(json.dumps(metadata))

    monkeypatch.setattr(wiring_subprocess.mp, "get_context", lambda _ctx: InlineContext())
    monkeypatch.setattr(wiring_subprocess, "_process_job_subprocess", _convert)
    return converted


def conversion_runner(engine: "Engine", config: "ConversionConfig") -> "StageRunner":
    """Return a runner over a conversion handler with a fake worker runtime, polling every 10 ms."""
    from aizk.conversion.handler import ConversionStageHandler
    from aizk.pipeline.runner import StageRunner
    from tests.fakes.runtime import fake_worker_runtime

    return StageRunner(ConversionStageHandler(config, runtime=fake_worker_runtime()), engine, poll_interval=0.01)
