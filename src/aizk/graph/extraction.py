"""Detect entity mentions in chunk text and extract them into mention drafts.

This module owns the extraction domain logic that sits between a persisted
:class:`~aizk.graph.datamodel.Chunk` and the append-only mention store
(:mod:`aizk.graph.mention_store`):

- :class:`Detection` — the in-memory contract a pluggable NER extractor emits: a
  surface form and the span it occupies in whatever text the extractor read.
- :class:`EntityExtractor` — the single substitutable access point through which
  extraction reaches its NER model, mirroring how :class:`aizk.graph.llm.LLMClient`
  is contextualization's single access point to its model. :class:`SpacyExtractor`
  and :class:`Gliner2Extractor` are the two pinned production implementations;
  tests supply a deterministic stand-in through the same interface.
- :data:`MATERIALIZER_VERSION` — the version of the deterministic post-NER logic this
  module implements (occurrence classification, span mapping), independent of
  which extractor produced the detections.
- :func:`select_extraction_input` — resolves the text a chunk is read from: its
  document's active contextualized variant when one is present and non-empty,
  else the chunk's own raw text.
- :func:`extract_chunk_mentions` — the pure, I/O-free extraction step: invokes
  the extractor once on the selected input text, classifies each detected
  surface form's occurrences against the raw chunk text, and emits one
  :class:`~aizk.graph.mention_store.MentionDraft` per occurrence (or one
  revision-anchored draft when the surface has none), ready for
  :func:`aizk.graph.mention_store.persist_mentions`.

Both pinned extractors are lazily imported (spaCy inside :class:`SpacyExtractor`,
GLiNER2 inside :class:`Gliner2Extractor`) so importing this module, and running
the contract suite against a deterministic stub, never requires the opt-in
``ner`` dependency group.
"""

from __future__ import annotations

import importlib.metadata
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

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
from aizk.graph.mention_store import MentionDraft
from aizk.pipeline.run import PipelineRun, RunStatus

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlmodel import Session

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


class Detection(BaseModel):
    """A single entity span an :class:`EntityExtractor` found in its input text.

    The extractor's output contract: ``surface_form`` is exactly the text at
    ``[span_start, span_end)`` in whatever text the extractor was invoked on —
    the boundary :func:`extract_chunk_mentions` relies on when re-deriving
    occurrences against the raw chunk text.

    Attributes:
        surface_form: The detected entity's exact surface text.
        span_start: Start offset of the detection in the extractor's input text.
        span_end: End offset of the detection in the extractor's input text.
    """

    model_config = ConfigDict(frozen=True)

    surface_form: str
    span_start: int
    span_end: int


@runtime_checkable
class EntityExtractor(Protocol):
    """The single substitutable access point through which extraction reaches its NER model.

    Implementations turn input text into a sequence of :class:`Detection`s.
    Extraction makes no NER call outside :meth:`extract`, so a recording or
    deterministic implementation observes and drives every invocation — the same
    seam :class:`aizk.graph.llm.LLMClient` provides for the model contextualization
    calls.
    """

    extractor_version: str
    """Identifies the extractor implementation, model, and configuration (for example
    entity-label schema) that produced a run's detections — encoded so it is honest
    to whatever is actually installed and configured."""

    def extract(self, text: str) -> "Sequence[Detection]":
        """Return the entity detections found in ``text``."""
        ...


class SpacyExtractor:
    """Production :class:`EntityExtractor` backed by spaCy's pinned ``en_core_web_sm`` pipeline.

    Lazily imports spaCy and loads the named pipeline on construction, so
    importing :mod:`aizk.graph.extraction` never requires the ``ner`` dependency
    group. ``extractor_version`` is derived from the loaded pipeline's own
    metadata (never hard-coded), so it stays honest to whatever pipeline is
    actually installed.
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
        pipeline_version = self._nlp.meta.get("version", "unknown")
        self.extractor_version = f"spacy/{model_name}@{pipeline_version}"

    def extract(self, text: str) -> "Sequence[Detection]":
        """Return spaCy's named-entity detections for ``text``, in document order."""
        doc = self._nlp(text)
        return [Detection(surface_form=ent.text, span_start=ent.start_char, span_end=ent.end_char) for ent in doc.ents]


class Gliner2Extractor:
    """Production :class:`EntityExtractor` backed by a locally pinned GLiNER2 model.

    Loads strictly from the local directory :data:`NerConfig.gliner2_model_dir`
    pre-fetched by the ``aizk-graph fetch-gliner2-weights`` setup step — never
    from the network at runtime. GLiNER2 is zero-shot: the entity-label schema
    it is invoked with is configuration, so it is encoded into
    ``extractor_version`` alongside the package version, the model repository,
    and the pinned revision the local directory was fetched at.
    """

    def __init__(
        self,
        *,
        settings: "NerConfig | None" = None,
        entity_labels: "Sequence[str]" = DEFAULT_GLINER2_LABELS,
    ) -> None:
        """Load the pinned GLiNER2 model from its configured local directory.

        Args:
            settings: NER settings; defaults to :class:`NerConfig` read from the
                environment.
            entity_labels: The zero-shot entity-label schema to extract with;
                defaults to :data:`DEFAULT_GLINER2_LABELS`.

        Raises:
            ImportError: If ``gliner2[local]`` is not installed, or the
                configured local model directory does not exist — both name the
                fix (the ``ner`` dependency group, or the pre-fetch command).
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
        try:
            package_version = importlib.metadata.version("gliner2")
        except importlib.metadata.PackageNotFoundError:  # pragma: no cover - defensive
            package_version = "unknown"
        labels_fingerprint = ",".join(sorted(self._entity_labels))
        self.extractor_version = (
            f"gliner2=={package_version}/{GLINER2_REPO_ID}@{settings.gliner2_revision}#labels={labels_fingerprint}"
        )

    def extract(self, text: str) -> "Sequence[Detection]":
        """Return GLiNER2's zero-shot entity detections for ``text``, sorted by position.

        Sorted by ``(span_start, span_end)`` regardless of the model's internal
        per-label grouping, so detection order — and therefore which detection is
        "first" for a repeated surface form — is deterministic in the input text
        alone.
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
    """The resolved text and locator :func:`extract_chunk_mentions` reads for one chunk.

    Produced by :func:`select_extraction_input`, the only place that decides
    raw-versus-contextualized input; :func:`extract_chunk_mentions` itself is
    pure and I/O-free, consuming whichever :class:`ExtractionInput` it is given.

    Attributes:
        text: The text to invoke the extractor on.
        input_kind: ``raw`` or ``contextualized`` — which text this is.
        context_version: The contextualized variant's version; present iff
            ``input_kind`` is ``contextualized``.
        contextualization_run_id: The contextualization run the variant was read
            from; present iff ``input_kind`` is ``contextualized``.
    """

    model_config = ConfigDict(frozen=True)

    text: str
    input_kind: InputKind
    context_version: int | None = Field(default=None)
    contextualization_run_id: int | None = Field(default=None)


def select_extraction_input(session: "Session", chunk: Chunk) -> ExtractionInput:
    """Resolve the text extraction reads for ``chunk``: its active contextualized variant, or raw fallback.

    Reads only from the chunk's document's **active** contextualization run
    (``pipeline_runs`` filtered to :data:`aizk.graph.contextualization.VARIANT_STAGE`,
    the chunk's ``source_id``, and ``status = active``) — a superseded run's
    variant is never read. A present variant with non-empty
    ``contextualized_text`` resolves to contextualized input, carrying that
    variant's ``context_version`` and the active run's id as the locator. An
    absent variant, or one present but empty (the already-self-contained case,
    whose consumed text equals the raw chunk text per the chunk-contextualization
    contract), resolves to raw input: the chunk's own text.

    Args:
        session: Active session; read-only.
        chunk: The persisted chunk to resolve input for.

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
    chunk_id: str,
    raw_chunk_text: str,
    extraction_input: ExtractionInput,
    extractor: "EntityExtractor",
) -> list[MentionDraft]:
    """Materialize one chunk's NER detections into mention drafts (pure, I/O-free).

    Invokes ``extractor.extract`` exactly once, on ``extraction_input.text`` —
    the single access point through which this stage reaches its NER model.
    Detections are grouped by exact ``surface_form``, keeping the **first**
    detection's span as that surface's ``input_span`` (same-surface repeats
    within one chunk share one disambiguation-context anchor by design — see
    design decision ``SpanCoordinateSystem``). For each distinct surface form,
    :func:`_raw_occurrences` searches ``raw_chunk_text``: one or more matches
    yield one source-anchored draft per occurrence (each with its own
    ``source_chunk_span`` and ``source_anchor_text``); zero matches yield one
    revision-anchored draft carrying the shared ``input_span`` and no source
    span. For raw input this branch is unreachable for a contract-honoring
    extractor — a detection's own span is then a raw occurrence; a violating
    detection is rejected downstream by
    :func:`aizk.graph.mention_store.persist_mentions` — so it only fires for
    contextualized input whose revision resolved a reference the raw chunk
    never states.

    Args:
        chunk_id: The raw chunk every emitted draft belongs to.
        raw_chunk_text: The chunk's raw text; occurrences are classified against
            this text regardless of what the extractor read.
        extraction_input: The resolved input (text, kind, and locator) to invoke
            the extractor on — see :func:`select_extraction_input`.
        extractor: The single injected NER access point.

    Returns:
        One :class:`~aizk.graph.mention_store.MentionDraft` per raw occurrence of
        each detected surface form (or one revision-anchored draft for a surface
        with none), in first-detected-surface order.
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
