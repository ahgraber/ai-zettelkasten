"""Store names for the document-summary and contextualized-variant runs."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from aizk.pipeline.run import PipelineRun, active_run

if TYPE_CHECKING:
    from uuid import UUID

    from sqlmodel import Session

#: Stage identifier for per-document summary runs in ``pipeline_runs``.
SUMMARY_STAGE = "document_summary"
#: Stage identifier for per-document contextualized-variant runs in ``pipeline_runs``.
VARIANT_STAGE = "chunk_contextualization"


class StalePlanError(RuntimeError):
    """Raised when a planned active run changes before persistence.

    The handler should retry the unit so it can resolve current state and regenerate
    outside the write transaction.
    """


class ContextSource(str, Enum):
    """Which representation of a chunk was used at the point of consumption."""

    RAW = "raw"
    CONTEXTUALIZED = "contextualized"


@dataclass(frozen=True)
class ResolvedChunkText:
    """A chunk's downstream text and the representation used to produce it.

    ``source`` identifies raw or contextualized input for downstream provenance.
    """

    text: str
    source: ContextSource


def _active_run(session: "Session", stage: str, scope_id: UUID) -> PipelineRun | None:
    """Return the active run for ``(stage, scope_id)``, or ``None``."""
    return active_run(session, stage, scope_id)


def resolve_chunk_text(
    working_text: str,
    *,
    contextualized_text: str | None,
    contextualization_enabled: bool,
) -> ResolvedChunkText:
    """Select raw or contextualized text for downstream consumption.

    Disabled contextualization or a missing variant returns raw text tagged
    :attr:`ContextSource.RAW`. An available variant returns contextualized text,
    including raw text tagged :attr:`ContextSource.CONTEXTUALIZED` when its
    revision is empty. Inspect revision content to detect whether text changed.
    """
    if not contextualization_enabled or contextualized_text is None:
        return ResolvedChunkText(working_text, ContextSource.RAW)
    if contextualized_text == "":
        return ResolvedChunkText(working_text, ContextSource.CONTEXTUALIZED)
    return ResolvedChunkText(contextualized_text, ContextSource.CONTEXTUALIZED)
