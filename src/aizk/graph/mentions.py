"""Persist run-scoped entity mentions and intra-chunk co-occurrence links.

The module defines extraction-key inputs, mention drafts, provenance validation,
run helpers, and the append-only persistence path. Drafts are validated before
writes, and persistence is idempotent within the run.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import TYPE_CHECKING, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import select
import xxhash

from aizk.graph.datamodel import (
    ANCHOR_KIND_REVISION,
    ANCHOR_KIND_SOURCE,
    INPUT_KIND_RAW,
    AnchorKind,
    Chunk,
    ContextualizedChunk,
    InputKind,
    Mention,
    MentionCooccurrence,
)
from aizk.pipeline.identity import DERIVATION_KEY_FORMAT, derivation_key, fingerprint
from aizk.pipeline.run import PipelineRun, RunStatus, active_run, reuse_or_record_run
from aizk.utilities.hashing import canonical_json

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlmodel import Session

#: Stage identifier stamped on extraction runs in ``pipeline_runs``.
MENTION_EXTRACTION_STAGE = "mention_extraction"

#: The extraction run's raw-vs-contextualized input toggle: ``contextualized``
#: reads a chunk's active contextualized variant when one is present and
#: non-empty (else raw fallback, per
#: :func:`~aizk.graph.extraction.select_extraction_input`); ``raw`` reads every
#: chunk's raw text unconditionally, regardless of any variant. Constrained to
#: exactly these two values so a typo'd policy is rejected by
#: :func:`open_extraction_run` rather than silently minting a novel derivation
#: key.
InputPolicy = Literal["contextualized", "raw"]
INPUT_POLICY_CONTEXTUALIZED: InputPolicy = "contextualized"
INPUT_POLICY_RAW: InputPolicy = "raw"
_VALID_INPUT_POLICIES: frozenset[str] = frozenset({INPUT_POLICY_CONTEXTUALIZED, INPUT_POLICY_RAW})


@dataclass(frozen=True)
class ResolvedExtractionConfig:
    """Immutable extraction configuration used to derive a run key.

    The configuration contains extractor, materializer, and input-policy versions.
    """

    extractor_version: str
    materializer_version: str
    input_policy: InputPolicy

    def __post_init__(self) -> None:
        """Validate the extraction input policy.

        Raises:
            ValueError: If ``input_policy`` is not ``raw`` or ``contextualized``.
        """
        if self.input_policy not in _VALID_INPUT_POLICIES:
            raise ValueError(
                f"input_policy {self.input_policy!r} is not one of {sorted(_VALID_INPUT_POLICIES)}; "
                "a typo'd policy must not silently mint a novel derivation key"
            )

    def config_fingerprint(self) -> str:
        """Return the fingerprint of this configuration: the ``config`` half of the extraction derivation key."""
        return fingerprint(
            {
                "extractor_version": self.extractor_version,
                "materializer_version": self.materializer_version,
                "input_policy": self.input_policy,
            }
        )


def extraction_derivation_key(*, input_fingerprint: str, config_fingerprint: str) -> str:
    """Return the derivation key for an extraction payload and configuration.

    Changing either fingerprint changes the key. An unchanged key allows the
    active run to be reused.
    """
    return derivation_key(inputs={"input": input_fingerprint, "config": config_fingerprint})


def derive_source_occurrence_key(
    *,
    chunk_id: UUID,
    source_span_start: int,
    source_span_end: int,
    source_anchor_text: str,
) -> str:
    """Return a deterministic cross-run key for a source occurrence.

    The key hashes the chunk ID, source span, and anchor text with canonical JSON.
    It is stable across processes, has a 16-character xxHash64 representation, and
    does not identify the persisted mention row.
    """
    canonical = json.dumps(
        [str(chunk_id), source_span_start, source_span_end, source_anchor_text],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return xxhash.xxh64(canonical.encode("utf-8")).hexdigest()


class MentionDraft(BaseModel):
    """In-memory entity mention produced by extraction and consumed by persistence.

    A draft records the raw chunk, detected surface, input locator, and input span.
    ``input_kind="raw"`` requires a source anchor. Contextualized input requires
    both ``context_version`` and ``contextualization_run_id``. A source anchor
    requires both source-span offsets and ``source_anchor_text``; a revision anchor
    has none of them.

    Attributes:
        chunk_id: Raw chunk identity.
        anchor_kind: ``source`` or ``revision``.
        surface_form: Non-empty detected surface text.
        input_kind: Text kind used for extraction.
        context_version: Context version for contextualized input.
        contextualization_run_id: Contextualization run for contextualized input.
        input_span_start: Start offset of the detection in the input text.
        input_span_end: End offset of the detection in the input text.
        source_span_start: Start offset in raw text for a source anchor.
        source_span_end: End offset in raw text for a source anchor.
        source_anchor_text: Raw text at the source span.
    """

    model_config = ConfigDict(frozen=True)

    chunk_id: UUID
    anchor_kind: AnchorKind
    surface_form: str = Field(min_length=1)
    input_kind: InputKind
    context_version: int | None = Field(default=None)
    contextualization_run_id: int | None = Field(default=None)
    input_span_start: int
    input_span_end: int
    source_span_start: int | None = Field(default=None)
    source_span_end: int | None = Field(default=None)
    source_anchor_text: str | None = Field(default=None)

    @model_validator(mode="after")
    def _check_anchor_class_congruence(self) -> "MentionDraft":
        """Require source provenance fields exactly for source anchors.

        Raises:
            ValueError: If the fields do not match ``anchor_kind``.
        """
        source_fields = (self.source_span_start, self.source_span_end, self.source_anchor_text)
        if self.anchor_kind == ANCHOR_KIND_SOURCE:
            if any(f is None for f in source_fields):
                raise ValueError(
                    "source-anchored draft requires source_span_start, source_span_end, "
                    "and source_anchor_text to all be present"
                )
        elif any(f is not None for f in source_fields):
            raise ValueError(
                "revision-anchored draft must not carry source_span_start, source_span_end, or source_anchor_text"
            )
        return self

    @model_validator(mode="after")
    def _check_context_locator_congruence(self) -> "MentionDraft":
        """Require context locator fields exactly for contextualized input.

        Raises:
            ValueError: If the context fields do not match ``input_kind``.
        """
        context_fields = (self.context_version, self.contextualization_run_id)
        if self.input_kind == INPUT_KIND_RAW:
            if any(f is not None for f in context_fields):
                raise ValueError("raw-input draft must not carry context_version or contextualization_run_id")
        elif any(f is None for f in context_fields):
            raise ValueError("contextualized-input draft requires both context_version and contextualization_run_id")
        return self

    def input_ref_json(self) -> str:
        """Serialize the input locator as canonical JSON.

        Raw input stores ``chunk_id``. Contextualized input also stores
        ``context_version`` and ``run_id``.
        """
        locator: dict[str, str | int] = {"chunk_id": str(self.chunk_id)}
        if self.input_kind != INPUT_KIND_RAW:
            locator["context_version"] = self.context_version  # type: ignore[assignment]
            locator["run_id"] = self.contextualization_run_id  # type: ignore[assignment]
        return canonical_json(locator, ensure_ascii=True)


def active_extraction_run(session: "Session", source_id: UUID) -> PipelineRun | None:
    """Return the active extraction run for a source (``source_id``), or ``None``."""
    return active_run(session, MENTION_EXTRACTION_STAGE, source_id)


def open_extraction_run(
    session: "Session",
    *,
    source_id: UUID,
    resolved: "ResolvedExtractionConfig",
    input_fingerprint: str,
) -> PipelineRun:
    """Open or reuse the source's active extraction run.

    An unchanged derivation key reuses the active run. A changed key creates a
    superseding run while preserving prior mentions. The function records version
    stamps from the same inputs as the key and does not commit.

    Args:
        session: Active session; the caller owns commit and rollback.
        source_id: Source identity for the run.
        resolved: Extraction configuration used for the key and stamps.
        input_fingerprint: Fingerprint of the extracted payload.

    Returns:
        The active or newly-created pipeline run.
    """
    config_fingerprint = resolved.config_fingerprint()
    key = extraction_derivation_key(input_fingerprint=input_fingerprint, config_fingerprint=config_fingerprint)
    version_stamps = {
        "extractor_version": resolved.extractor_version,
        "materializer_version": resolved.materializer_version,
        "input_policy": resolved.input_policy,
        "input_fingerprint": input_fingerprint,
        "config_fingerprint": config_fingerprint,
        "derivation_key_format": DERIVATION_KEY_FORMAT,
    }
    return reuse_or_record_run(
        session,
        stage=MENTION_EXTRACTION_STAGE,
        scope_id=source_id,
        derivation_key=key,
        version_stamps=version_stamps,
    )


def _existing_mention(session: "Session", run_id: int, draft: "MentionDraft") -> Mention | None:
    """Return the persisted mention matching ``draft``'s within-run class-identity tuple, if any.

    Source-anchored identity is ``(run_id, chunk_id, source_span_start,
    source_span_end, surface_form)``; revision-anchored identity is ``(run_id,
    chunk_id, surface_form)`` — matching the schema's per-class partial unique
    indexes.
    """
    if draft.anchor_kind == ANCHOR_KIND_SOURCE:
        return session.exec(
            select(Mention).where(
                Mention.run_id == run_id,
                Mention.chunk_id == draft.chunk_id,
                Mention.source_span_start == draft.source_span_start,
                Mention.source_span_end == draft.source_span_end,
                Mention.surface_form == draft.surface_form,
            )
        ).one_or_none()
    return session.exec(
        select(Mention).where(
            Mention.run_id == run_id,
            Mention.chunk_id == draft.chunk_id,
            Mention.surface_form == draft.surface_form,
            # The class predicate is load-bearing: without it a source-anchored
            # row sharing (run, chunk, surface_form) would shadow the revision
            # draft and its mention would never be persisted.
            Mention.anchor_kind == ANCHOR_KIND_REVISION,
        )
    ).one_or_none()


def _class_tuple(run_id: int, draft: "MentionDraft") -> tuple[object, ...]:
    """Return ``draft``'s within-run class-identity tuple, matching :func:`_existing_mention`'s predicate.

    Used to key the intra-batch resolution dict in :func:`persist_mentions`, so a
    class tuple repeated within one call resolves to the same in-memory row
    without a redundant store lookup.
    """
    if draft.anchor_kind == ANCHOR_KIND_SOURCE:
        return (
            run_id,
            ANCHOR_KIND_SOURCE,
            draft.chunk_id,
            draft.source_span_start,
            draft.source_span_end,
            draft.surface_form,
        )
    return (run_id, ANCHOR_KIND_REVISION, draft.chunk_id, draft.surface_form)


def _contextualized_locator(draft: "MentionDraft") -> tuple[UUID, int, int]:
    """Return a contextualized draft's ``(chunk_id, contextualization_run_id, context_version)`` locator."""
    assert draft.contextualization_run_id is not None  # noqa: S101 — validator-enforced
    assert draft.context_version is not None  # noqa: S101 — validator-enforced
    return (draft.chunk_id, draft.contextualization_run_id, draft.context_version)


def _consumed_text(draft: "MentionDraft", chunk: Chunk, variant: ContextualizedChunk | None) -> str:
    """Return the text indexed by a draft's input span.

    Raw drafts use chunk text. Contextualized drafts use the resolved non-empty
    variant.
    """
    if draft.input_kind == INPUT_KIND_RAW:
        return chunk.text
    assert variant is not None  # noqa: S101 — resolved by the caller before this is invoked
    return variant.contextualized_text


def _validate_provenance(session: "Session", *, run: PipelineRun, mentions: "Sequence[MentionDraft]") -> None:
    """Validate draft provenance before persistence.

    The checks cover run status, chunk ownership, input-kind congruence,
    contextualized locator resolution, and source and input spans. The function
    raises on the first failure and loads each distinct chunk and variant at most
    once.

    Raises:
        ValueError: If any provenance or span check fails.
    """
    if run.stage != MENTION_EXTRACTION_STAGE or run.status != RunStatus.ACTIVE:
        raise ValueError(
            f"run {run.id!r} is not an active {MENTION_EXTRACTION_STAGE!r} run "
            f"(stage={run.stage!r}, status={run.status!r}); mentions may only be persisted "
            "under an active extraction run"
        )

    chunk_ids = sorted({d.chunk_id for d in mentions})
    chunks_by_id = {c.chunk_id: c for c in session.exec(select(Chunk).where(Chunk.chunk_id.in_(chunk_ids))).all()}
    missing_chunk_ids = sorted(cid for cid in chunk_ids if cid not in chunks_by_id)
    if missing_chunk_ids:
        raise ValueError(
            f"chunk_id(s) {missing_chunk_ids} do not resolve to a persisted chunk; "
            "a mention's source chunk must be resolvable"
        )

    cross_source = sorted({d.chunk_id for d in mentions if chunks_by_id[d.chunk_id].source_id != run.scope_id})
    if cross_source:
        raise ValueError(
            f"chunk_id(s) {cross_source} belong to a source other than run.scope_id={run.scope_id!r}; "
            "a mention must be persisted under its own source's run"
        )

    invalid_raw = [
        d.chunk_id for d in mentions if d.input_kind == INPUT_KIND_RAW and d.anchor_kind != ANCHOR_KIND_SOURCE
    ]
    if invalid_raw:
        raise ValueError(
            f"draft(s) for chunk_id(s) {invalid_raw} have input_kind='raw' but are not source-anchored; "
            "a mention read from raw input must be source-anchored"
        )

    contextualized_drafts = [d for d in mentions if d.input_kind != INPUT_KIND_RAW]
    locators = sorted({_contextualized_locator(d) for d in contextualized_drafts})
    variants_by_locator: dict[tuple[UUID, int, int], ContextualizedChunk] = {}
    for chunk_id, contextualization_run_id, context_version in locators:
        variant = session.exec(
            select(ContextualizedChunk).where(
                ContextualizedChunk.run_id == contextualization_run_id,
                ContextualizedChunk.chunk_id == chunk_id,
                ContextualizedChunk.context_version == context_version,
            )
        ).one_or_none()
        if variant is not None:
            variants_by_locator[(chunk_id, contextualization_run_id, context_version)] = variant
    dangling_locators = [loc for loc in locators if loc not in variants_by_locator]
    if dangling_locators:
        raise ValueError(
            f"contextualized locator(s) {dangling_locators} (chunk_id, contextualization_run_id, "
            "context_version) do not resolve to a persisted ContextualizedChunk; "
            "the stored input_ref would dangle"
        )

    present_empty_locators = [loc for loc, variant in variants_by_locator.items() if variant.contextualized_text == ""]
    if present_empty_locators:
        raise ValueError(
            f"contextualized locator(s) {present_empty_locators} (chunk_id, contextualization_run_id, "
            "context_version) resolve to a present-empty variant (contextualized_text=''): the consumed "
            "text is the raw chunk text, so the mention must be recorded with input_kind='raw' and the "
            "chunk as its input_ref"
        )

    source_drafts = [d for d in mentions if d.anchor_kind == ANCHOR_KIND_SOURCE]
    bad_source_spans: list[tuple[UUID, int | None, int | None]] = []
    for d in source_drafts:
        assert d.source_span_start is not None  # noqa: S101 — validator-enforced
        assert d.source_span_end is not None  # noqa: S101 — validator-enforced
        chunk = chunks_by_id[d.chunk_id]
        text_len = len(chunk.text)
        if not (0 <= d.source_span_start < d.source_span_end <= text_len):
            bad_source_spans.append((d.chunk_id, d.source_span_start, d.source_span_end))
            continue
        sliced = chunk.text[d.source_span_start : d.source_span_end]
        if sliced != d.source_anchor_text or sliced != d.surface_form:
            bad_source_spans.append((d.chunk_id, d.source_span_start, d.source_span_end))
    if bad_source_spans:
        raise ValueError(
            f"source-anchored draft(s) {bad_source_spans} (chunk_id, source_span_start, source_span_end) "
            "have a span that is out of bounds, inverted, or does not slice the raw chunk text to both "
            "source_anchor_text and surface_form"
        )

    bad_input_spans: list[tuple[UUID, int, int]] = []
    for d in mentions:
        chunk = chunks_by_id[d.chunk_id]
        variant = variants_by_locator.get(_contextualized_locator(d)) if d.input_kind != INPUT_KIND_RAW else None
        consumed_text = _consumed_text(d, chunk, variant)
        text_len = len(consumed_text)
        if not (0 <= d.input_span_start < d.input_span_end <= text_len):
            bad_input_spans.append((d.chunk_id, d.input_span_start, d.input_span_end))
            continue
        if consumed_text[d.input_span_start : d.input_span_end] != d.surface_form:
            bad_input_spans.append((d.chunk_id, d.input_span_start, d.input_span_end))
    if bad_input_spans:
        raise ValueError(
            f"draft(s) {bad_input_spans} (chunk_id, input_span_start, input_span_end) have an input_span "
            "that is out of bounds or does not slice the consumed input text to surface_form"
        )


def persist_mentions(
    session: "Session",
    *,
    run: PipelineRun,
    mentions: "Sequence[MentionDraft]",
) -> list[Mention]:
    """Persist mention drafts under an extraction run.

    The function validates the full batch before writing, reuses existing mentions
    by anchor-class identity, and inserts missing rows. It is append-only and
    idempotent. Co-occurrence links cover all mentions in each touched chunk and
    use conflict-safe insertion.

    The function flushes once before linking and does not commit.

    Args:
        session: Active session; the caller owns commit and rollback.
        run: Extraction run for the drafts.
        mentions: The mention drafts to persist.

    Returns:
        Persisted mention rows in draft order.

    Raises:
        ValueError: If provenance or span validation fails.
    """
    _validate_provenance(session, run=run, mentions=mentions)

    assert run.id is not None  # noqa: S101 — a persisted run always carries an id

    persisted: list[Mention] = []
    touched_chunks: set[UUID] = set()
    # Rows already resolved (reused from the store or newly staged) during this
    # call, keyed by class-identity tuple, so a repeated tuple resolves from
    # memory instead of a redundant store lookup or a flush per draft.
    resolved_by_class_tuple: dict[tuple[object, ...], Mention] = {}
    for draft in mentions:
        class_tuple = _class_tuple(run.id, draft)
        row = resolved_by_class_tuple.get(class_tuple)
        if row is None:
            row = _existing_mention(session, run.id, draft)

        if row is None:
            source_occurrence_key = None
            if draft.anchor_kind == ANCHOR_KIND_SOURCE:
                assert draft.source_span_start is not None  # noqa: S101 — validator-enforced
                assert draft.source_span_end is not None  # noqa: S101 — validator-enforced
                assert draft.source_anchor_text is not None  # noqa: S101 — validator-enforced
                source_occurrence_key = derive_source_occurrence_key(
                    chunk_id=draft.chunk_id,
                    source_span_start=draft.source_span_start,
                    source_span_end=draft.source_span_end,
                    source_anchor_text=draft.source_anchor_text,
                )
            row = Mention(
                mention_id=uuid4(),
                run_id=run.id,
                chunk_id=draft.chunk_id,
                anchor_kind=draft.anchor_kind,
                surface_form=draft.surface_form,
                input_kind=draft.input_kind,
                input_ref=draft.input_ref_json(),
                input_span_start=draft.input_span_start,
                input_span_end=draft.input_span_end,
                source_span_start=draft.source_span_start,
                source_span_end=draft.source_span_end,
                source_occurrence_key=source_occurrence_key,
            )
            session.add(row)

        resolved_by_class_tuple[class_tuple] = row
        persisted.append(row)
        touched_chunks.add(draft.chunk_id)

    session.flush()

    for chunk_id in touched_chunks:
        _link_chunk_cooccurrences(session, run_id=run.id, chunk_id=chunk_id)

    return persisted


def _link_chunk_cooccurrences(session: "Session", *, run_id: int, chunk_id: UUID) -> None:
    """Record all co-occurrence pairs for a run's chunk.

    The function reads all mentions in the chunk and inserts canonical pairs with
    conflict-safe semantics.
    """
    chunk_mention_ids = list(
        session.exec(select(Mention.mention_id).where(Mention.run_id == run_id, Mention.chunk_id == chunk_id)).all()
    )
    if len(chunk_mention_ids) < 2:
        return

    pairs: list[dict[str, "int | UUID"]] = []
    for i, id_a in enumerate(chunk_mention_ids):
        for id_b in chunk_mention_ids[i + 1 :]:
            lo, hi = (id_a, id_b) if id_a < id_b else (id_b, id_a)
            pairs.append({"run_id": run_id, "mention_id_lo": lo, "mention_id_hi": hi, "chunk_id": chunk_id})

    statement = (
        sqlite_insert(MentionCooccurrence)
        .values(pairs)
        .on_conflict_do_nothing(index_elements=["run_id", "mention_id_lo", "mention_id_hi"])
    )
    session.execute(statement)
