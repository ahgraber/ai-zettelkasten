"""Extract entity mentions from chunk text into run-scoped mention drafts.

The module defines the extractor interface, input selection, deterministic
detection materialization, and pinned spaCy and GLiNER2 implementations. It does
not load optional NER dependencies until a production extractor is constructed.
"""

from __future__ import annotations

import importlib.metadata
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import select

from aizk.graph.config import NerConfig
from aizk.graph.contextualization import VARIANT_STAGE
from aizk.graph.datamodel import (
    ANCHOR_KIND_REVISION,
    ANCHOR_KIND_SOURCE,
    INPUT_KIND_CONTEXTUALIZED,
    INPUT_KIND_RAW,
    Chunk,
    ContextualizedChunk,
    InputKind,
)
from aizk.graph.mention_store import MentionDraft, ResolvedExtractionConfig
from aizk.pipeline.run import PipelineRun, RunStatus

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlmodel import Session

    from aizk.graph.config import ExtractionConfig

#: Version of the deterministic post-NER materialization logic: occurrence
#: classification (searching the raw chunk text for a detected surface form's
#: occurrences and emitting one source-anchored mention per occurrence, or one
#: revision-anchored mention when none), the input→raw span mapping, and the
#: store-side materialization those drafts feed — co-occurrence linking and
#: surrogate-id / ``source_occurrence_key`` assignment in
#: :mod:`aizk.graph.mention_store`. A change to any of that logic bumps this
#: version, independent of ``extractor_version`` (which versions the NER model,
#: not this logic).
MATERIALIZER_VERSION = "1"

#: The HuggingFace repository the pinned :class:`Gliner2Extractor` weights are
#: fetched from. Not itself configurable — the model identity is a pinned
#: dependency; only its local location and pinned revision are (see
#: :class:`~aizk.graph.config.NerConfig`).
GLINER2_REPO_ID = "fastino/gliner2-base-v1"

#: Default zero-shot entity-label schema :class:`Gliner2Extractor` extracts
#: with. Part of the extractor's configuration: encoded into
#: ``extractor_version`` alongside the package version, model, and revision, so
#: a schema change is an observable, versioned run input.
DEFAULT_GLINER2_LABELS: "tuple[str, ...]" = (
    "person",
    "organization",
    "location",
    "product",
    "event",
    "concept",
)


def spacy_extractor_version(model_name: str) -> str:
    """Return the extractor version for a spaCy pipeline without loading it.

    The version includes the installed package version.

    Raises:
        importlib.metadata.PackageNotFoundError: If ``model_name`` is not installed.
    """
    return f"spacy/{model_name}@{importlib.metadata.version(model_name)}"


def gliner2_extractor_version(settings: "NerConfig", entity_labels: "Sequence[str]" = DEFAULT_GLINER2_LABELS) -> str:
    """Return the GLiNER2 extractor version without loading the model.

    The version includes the installed package version, model revision, and
    entity-label set. Label order does not affect the result. The function makes
    no network or model-weight access.
    """
    try:
        package_version = importlib.metadata.version("gliner2")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover - defensive
        package_version = "unknown"
    labels_fingerprint = ",".join(sorted(entity_labels))
    return f"gliner2=={package_version}/{GLINER2_REPO_ID}@{settings.gliner2_revision}#labels={labels_fingerprint}"


def resolve_extraction_config(config: "ExtractionConfig") -> ResolvedExtractionConfig:
    """Resolve extractor and input policy into a :class:`ResolvedExtractionConfig`.

    The function does not construct an extractor or load model weights.
    """
    if config.extractor == "spacy":
        extractor_version = spacy_extractor_version("en_core_web_sm")
    else:
        extractor_version = gliner2_extractor_version(NerConfig())
    return ResolvedExtractionConfig(
        extractor_version=extractor_version,
        materializer_version=MATERIALIZER_VERSION,
        input_policy=config.input_policy,
    )


class Detection(BaseModel):
    """Entity span returned by an :class:`EntityExtractor`.

    ``surface_form`` equals the input text slice from ``span_start`` to
    ``span_end``.
    """

    model_config = ConfigDict(frozen=True)

    surface_form: str
    span_start: int
    span_end: int


@runtime_checkable
class EntityExtractor(Protocol):
    """Protocol for the NER extractor used by the extraction stage.

    Implementations expose an ``extractor_version`` and return detections from
    :meth:`extract`.
    """

    extractor_version: str
    """Identifies the extractor implementation, model, and configuration (for example
    entity-label schema) that produced a run's detections — encoded so it is honest
    to whatever is actually installed and configured."""

    def extract(self, text: str) -> "Sequence[Detection]":
        """Return the entity detections found in ``text``."""
        ...


class SpacyExtractor:
    """Production :class:`EntityExtractor` backed by a local spaCy pipeline.

    The module imports spaCy lazily. Construction loads ``model_name`` and sets
    ``extractor_version`` from the installed package version.
    """

    def __init__(self, model_name: str = "en_core_web_sm") -> None:
        """Load the named spaCy pipeline.

        Args:
            model_name: The spaCy pipeline package to load; defaults to the
                pinned ``en_core_web_sm``.

        Raises:
            ImportError: If spaCy or the named pipeline is not installed —
                names the ``ner`` dependency group as the fix.
        """
        try:
            import spacy
        except ImportError as exc:
            raise ImportError(
                "SpacyExtractor requires the 'ner' dependency group; install it with 'uv sync --group ner'"
            ) from exc
        try:
            self._nlp = spacy.load(model_name)
        except OSError as exc:
            raise ImportError(
                f"spaCy pipeline {model_name!r} is not installed; install the 'ner' dependency "
                "group with 'uv sync --group ner'"
            ) from exc
        self.extractor_version = spacy_extractor_version(model_name)

    def extract(self, text: str) -> "Sequence[Detection]":
        """Return spaCy's named-entity detections for ``text``, in document order."""
        doc = self._nlp(text)
        return [Detection(surface_form=ent.text, span_start=ent.start_char, span_end=ent.end_char) for ent in doc.ents]


class Gliner2Extractor:
    """Production :class:`EntityExtractor` backed by a local GLiNER2 model.

    Construction loads weights from ``NerConfig.gliner2_model_dir`` without
    network access. The configured entity-label schema contributes to
    ``extractor_version``.
    """

    def __init__(
        self,
        *,
        settings: "NerConfig | None" = None,
        entity_labels: "Sequence[str]" = DEFAULT_GLINER2_LABELS,
    ) -> None:
        """Load the pinned GLiNER2 model from its configured local directory.

        Args:
            settings: NER settings; defaults to :class:`NerConfig` from the environment.
            entity_labels: Entity labels for zero-shot extraction.

        Raises:
            ImportError: If the GLiNER2 dependency, model directory, or model artifact
                is unavailable.
        """
        settings = settings or NerConfig()
        try:
            from gliner2 import GLiNER2
        except ImportError as exc:
            raise ImportError(
                "Gliner2Extractor requires the 'ner' dependency group; install it with 'uv sync --group ner'"
            ) from exc
        model_dir = Path(settings.gliner2_model_dir)
        if not model_dir.exists():
            raise ImportError(
                f"GLiNER2 weights are not present at {model_dir}; run "
                "'aizk-graph fetch-gliner2-weights' to pre-fetch the pinned revision "
                f"({settings.gliner2_revision}) before constructing Gliner2Extractor"
            )
        self._model = GLiNER2.from_pretrained(str(model_dir))
        self._entity_labels: tuple[str, ...] = tuple(entity_labels)
        self.extractor_version = gliner2_extractor_version(settings, self._entity_labels)

    def extract(self, text: str) -> "Sequence[Detection]":
        """Return GLiNER2 detections sorted by ``(span_start, span_end)``.

        Sorting makes detection order deterministic when the model groups entities by
        label.
        """
        if not text:
            return []
        result = self._model.extract_entities(text, list(self._entity_labels), include_spans=True)
        detections = [
            Detection(surface_form=entity["text"], span_start=entity["start"], span_end=entity["end"])
            for entities in result.get("entities", {}).values()
            for entity in entities
        ]
        return sorted(detections, key=lambda d: (d.span_start, d.span_end))


class ExtractionInput(BaseModel):
    """Resolved text and locator used to extract mentions from one chunk.

    ``input_kind`` is ``raw`` or ``contextualized``. Contextualized input includes
    both ``context_version`` and ``contextualization_run_id``; raw input includes
    neither.
    """

    model_config = ConfigDict(frozen=True)

    text: str
    input_kind: InputKind
    context_version: int | None = Field(default=None)
    contextualization_run_id: int | None = Field(default=None)


def select_extraction_input(session: "Session", chunk: Chunk) -> ExtractionInput:
    """Resolve the active input text for ``chunk``.

    The function reads the active contextualization variant for the chunk's
    source. A non-empty variant returns contextualized input with its version and
    run ID. An absent or empty variant returns the raw chunk text.

    Args:
        session: Active, read-only session.
        chunk: The persisted chunk to resolve.

    Returns:
        The resolved :class:`ExtractionInput`.
    """
    active_run = session.exec(
        select(PipelineRun).where(
            PipelineRun.stage == VARIANT_STAGE,
            PipelineRun.scope_id == chunk.source_id,
            PipelineRun.status == RunStatus.ACTIVE,
        )
    ).one_or_none()
    if active_run is not None and active_run.id is not None:
        variant = session.exec(
            select(ContextualizedChunk).where(
                ContextualizedChunk.run_id == active_run.id,
                ContextualizedChunk.chunk_id == chunk.chunk_id,
            )
        ).one_or_none()
        if variant is not None and variant.contextualized_text != "":
            return ExtractionInput(
                text=variant.contextualized_text,
                input_kind=INPUT_KIND_CONTEXTUALIZED,
                context_version=variant.context_version,
                contextualization_run_id=active_run.id,
            )
    return ExtractionInput(text=chunk.text, input_kind=INPUT_KIND_RAW)


def _raw_occurrences(raw_chunk_text: str, surface_form: str) -> list[tuple[int, int]]:
    """Return every non-overlapping occurrence of ``surface_form`` in ``raw_chunk_text``.

    Exact string match via a ``str.find`` loop, advancing past each match so
    occurrences never overlap — this is the classification rule the
    source-versus-revision anchor split is built on; any change to it bumps
    :data:`MATERIALIZER_VERSION`. An empty ``surface_form`` (never legal on a
    persisted mention) matches everywhere in ``str.find``, so it is guarded
    here to avoid a non-advancing loop rather than by trusting callers.
    """
    if not surface_form:
        return []
    occurrences: list[tuple[int, int]] = []
    start = 0
    while (idx := raw_chunk_text.find(surface_form, start)) != -1:
        occurrences.append((idx, idx + len(surface_form)))
        start = idx + len(surface_form)
    return occurrences


def extract_chunk_mentions(
    *,
    chunk_id: UUID,
    raw_chunk_text: str,
    extraction_input: ExtractionInput,
    extractor: "EntityExtractor",
) -> list[MentionDraft]:
    """Materialize extractor detections as mention drafts without I/O.

    The extractor is called once on ``extraction_input.text``. The function keeps
    the first span for each exact surface form, then emits one source-anchored
    draft per raw occurrence or one revision-anchored draft when no raw occurrence
    exists. It returns drafts in first-detected-surface order.

    Args:
        chunk_id: The chunk identity for every draft.
        raw_chunk_text: Text used to classify source occurrences.
        extraction_input: Text and locator passed to the extractor.
        extractor: The injected NER implementation.

    Returns:
        The resulting mention drafts in first-detected-surface order.
    """
    detections = extractor.extract(extraction_input.text)

    first_span_by_surface: dict[str, tuple[int, int]] = {}
    for detection in detections:
        first_span_by_surface.setdefault(detection.surface_form, (detection.span_start, detection.span_end))

    drafts: list[MentionDraft] = []
    for surface_form, (input_start, input_end) in first_span_by_surface.items():
        occurrences = _raw_occurrences(raw_chunk_text, surface_form)
        if occurrences:
            for source_start, source_end in occurrences:
                drafts.append(
                    MentionDraft(
                        chunk_id=chunk_id,
                        anchor_kind=ANCHOR_KIND_SOURCE,
                        surface_form=surface_form,
                        input_kind=extraction_input.input_kind,
                        context_version=extraction_input.context_version,
                        contextualization_run_id=extraction_input.contextualization_run_id,
                        input_span_start=input_start,
                        input_span_end=input_end,
                        source_span_start=source_start,
                        source_span_end=source_end,
                        source_anchor_text=raw_chunk_text[source_start:source_end],
                    )
                )
        else:
            drafts.append(
                MentionDraft(
                    chunk_id=chunk_id,
                    anchor_kind=ANCHOR_KIND_REVISION,
                    surface_form=surface_form,
                    input_kind=extraction_input.input_kind,
                    context_version=extraction_input.context_version,
                    contextualization_run_id=extraction_input.contextualization_run_id,
                    input_span_start=input_start,
                    input_span_end=input_end,
                )
            )
    return drafts
