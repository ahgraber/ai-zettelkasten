"""Persist entity mentions and their intra-chunk co-occurrence under extraction runs.

This module owns the mention store's write path:

- :func:`extraction_derivation_key` / :func:`derive_source_occurrence_key` — the
  deterministic key derivations (mirroring
  :func:`aizk.graph.persistence._chunking_derivation_key`).
- :class:`MentionDraft` — the in-memory contract extraction emits, mirroring how
  :class:`aizk.chunking.Chunk` is the splitter's in-memory contract.
- :func:`active_extraction_run` / :func:`open_extraction_run` — thin wrappers
  over the shared ``pipeline_runs`` primitive (mirroring
  :func:`aizk.graph.persistence.active_chunking_run` /
  :func:`aizk.pipeline.run.reuse_or_record_run`), scoping an extraction run to a
  source.
- :func:`persist_mentions` — the append-only, idempotent write path that turns
  drafts into :class:`~aizk.graph.datamodel.Mention` rows and their intra-chunk
  :class:`~aizk.graph.datamodel.MentionCooccurrence` links, validating each
  draft's provenance (run congruence, chunk resolution, source scoping,
  contextualized-locator resolution, and span integrity) against authoritative
  rows before any write.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

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
from aizk.pipeline.run import PipelineRun, RunStatus, reuse_or_record_run

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


def extraction_derivation_key(
    *,
    extractor_version: str,
    materializer_version: str,
    input_policy: InputPolicy,
    upstream_derivation_key: str,
) -> str:
    """Canonical derivation key for an extraction run.

    Encodes exactly the four semantic inputs that determine the emitted mention
    set: the NER extractor's version, the deterministic post-NER materialization
    logic's version, the raw-vs-contextualized ``input_policy`` toggle, and the
    consumed upstream run's derivation key — the source's active contextualization
    run's key when ``input_policy`` is contextualized, else its active chunking
    run's key, so upstream invalidation (a re-chunk or a re-contextualization)
    propagates into a superseding extraction run. No local surrogate id (a
    ``run_id`` or row id) enters the key, so it recomputes identically on any
    backend and across a logical migration.

    Changing any one of the four inputs yields a different key, so the source's
    next extraction opens a run that supersedes the prior one; an unchanged key
    reuses the active run.
    """
    return json.dumps(
        {
            "extractor_version": extractor_version,
            "materializer_version": materializer_version,
            "input_policy": input_policy,
            "upstream_derivation_key": upstream_derivation_key,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def derive_source_occurrence_key(
    *,
    chunk_id: str,
    source_span_start: int,
    source_span_end: int,
    source_anchor_text: str,
) -> str:
    """Derive a source-anchored mention's cross-run-stable occurrence key.

    A deterministic, cross-process-stable hash of the four inputs — the chunk the
    occurrence sits in, its span, and the raw text at that span — computed the same
    way :func:`aizk.chunking.datamodel.derive_chunk_content_key` derives a chunk's
    portable content key: a compact canonical JSON array hashed with xxh64. Never
    Python's builtin ``hash()``, which is randomized per-process and not a legal
    persisted or cross-process value.

    This key is a non-primary, cross-run diagnostic: two extraction runs that
    both detect the same source occurrence yield distinct :class:`Mention` rows
    (each belonging to its own run) with equal ``source_occurrence_key``s, which
    supports gold-set alignment and run-to-run diffing. A mention's identity
    remains its surrogate ``mention_id``, never this key. The key takes no run
    parameter, so it is run-independent by construction.

    Args:
        chunk_id: The raw chunk the occurrence sits in.
        source_span_start: Start offset of the occurrence in the raw chunk text.
        source_span_end: End offset of the occurrence in the raw chunk text.
        source_anchor_text: The raw text at ``[source_span_start, source_span_end)``.

    Returns:
        Hex-encoded xxHash64 digest (16 characters).
    """
    canonical = json.dumps(
        [chunk_id, source_span_start, source_span_end, source_anchor_text],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return xxhash.xxh64(canonical.encode("utf-8")).hexdigest()


class MentionDraft(BaseModel):
    """An in-memory entity-mention detection, emitted by extraction and consumed by :func:`persist_mentions`.

    Mirrors how :class:`aizk.chunking.Chunk` is the splitter's in-memory contract:
    extraction is a pure, I/O-free producer of drafts, and this module is the
    distinct component that durably stores them and assigns each mention its
    surrogate identity.

    A draft's input locator identifies the exact text it was read from —
    ``chunk_id`` alone for raw input, or ``chunk_id`` plus ``context_version`` and
    ``contextualization_run_id`` for a contextualized variant — which
    :func:`persist_mentions` serializes to the ``input_ref`` canonical-JSON column
    (``{"chunk_id": ...}`` or ``{"chunk_id": ..., "context_version": ...,
    "run_id": ...}``, matching :class:`~aizk.graph.datamodel.Mention`'s documented
    shape).

    ``source_anchor_text`` is the raw text at ``[source_span_start,
    source_span_end)`` — carried here only to derive ``source_occurrence_key`` at
    persistence time; it is not itself a stored column.

    Attributes:
        chunk_id: The raw :class:`~aizk.graph.datamodel.Chunk` this detection
            belongs to.
        anchor_kind: ``source`` (the surface form occurs in the raw chunk text) or
            ``revision`` (resolved only by a contextualized revision).
        surface_form: The detected entity surface text. Must be non-empty: an
            empty surface is never a detection.
        input_kind: ``raw`` or ``contextualized`` — which text the detection was
            read from.
        context_version: The contextualized variant's version, present iff
            ``input_kind`` is ``contextualized``.
        contextualization_run_id: The contextualization run the variant was read
            from, present iff ``input_kind`` is ``contextualized``.
        input_span_start: Start offset of the detection in the input text.
        input_span_end: End offset of the detection in the input text.
        source_span_start: Start offset of the occurrence in the raw chunk text;
            present iff ``anchor_kind`` is ``source``.
        source_span_end: End offset of the occurrence in the raw chunk text;
            present iff ``anchor_kind`` is ``source``.
        source_anchor_text: The raw chunk text at
            ``[source_span_start, source_span_end)``; present iff ``anchor_kind``
            is ``source``.
    """

    model_config = ConfigDict(frozen=True)

    chunk_id: str
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
        """Enforce that source fields are present iff ``anchor_kind`` is ``source``.

        A source-anchored draft must carry its span pair and anchor text together;
        a revision-anchored draft must carry none of them — partial provenance
        (a span with no anchor text, or vice versa) is never a legal draft.
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
        """Enforce that ``context_version``/``contextualization_run_id`` are present iff contextualized.

        A raw-input draft's locator is ``chunk_id`` alone; a contextualized-input
        draft's locator additionally pins the exact variant read, so both fields
        must be present together.
        """
        context_fields = (self.context_version, self.contextualization_run_id)
        if self.input_kind == INPUT_KIND_RAW:
            if any(f is not None for f in context_fields):
                raise ValueError("raw-input draft must not carry context_version or contextualization_run_id")
        elif any(f is None for f in context_fields):
            raise ValueError("contextualized-input draft requires both context_version and contextualization_run_id")
        return self

    def input_ref_json(self) -> str:
        """Serialize this draft's input locator to the canonical ``input_ref`` JSON text.

        Raw input serializes to ``{"chunk_id": ...}``; contextualized input to
        ``{"chunk_id": ..., "context_version": ..., "run_id": ...}`` — matching
        :class:`~aizk.graph.datamodel.Mention`'s documented ``input_ref`` shape.
        """
        locator: dict[str, str | int] = {"chunk_id": self.chunk_id}
        if self.input_kind != INPUT_KIND_RAW:
            locator["context_version"] = self.context_version  # type: ignore[assignment]
            locator["run_id"] = self.contextualization_run_id  # type: ignore[assignment]
        return json.dumps(locator, sort_keys=True, separators=(",", ":"))


def active_extraction_run(session: "Session", source_id: str) -> PipelineRun | None:
    """Return the active extraction run for a source (``source_id``), or ``None``."""
    return session.exec(
        select(PipelineRun).where(
            PipelineRun.stage == MENTION_EXTRACTION_STAGE,
            PipelineRun.scope_id == source_id,
            PipelineRun.status == RunStatus.ACTIVE,
        )
    ).one_or_none()


def open_extraction_run(
    session: "Session",
    *,
    source_id: str,
    extractor_version: str,
    materializer_version: str,
    input_policy: InputPolicy,
    upstream_derivation_key: str,
) -> PipelineRun:
    """Open (or reuse) a source's extraction run under the shared run primitive.

    Scoped per source (``scope_id = str(source_id)``), exactly like the chunking
    and contextualization runs upstream. The derivation key and the recorded
    ``version_stamps`` are both derived internally from the same semantic inputs
    (mirroring how :func:`aizk.graph.persistence.persist_chunks` derives its run's
    key and stamps together), so the two can never be missing or mutually
    inconsistent. An unchanged derivation key reuses the source's active
    extraction run (a retry or unchanged re-entry); a changed key opens a new
    active run that supersedes the prior one — a pure status transition, so the
    prior run's mentions remain present and unmodified.

    ``input_policy`` is validated against :data:`InputPolicy`'s two legal values
    before it can influence the derivation key, so a typo'd policy fails loudly
    here rather than silently minting a novel, unreachable key.

    Does **not** commit; the caller owns the surrounding transaction.

    Args:
        session: Active session; the caller owns commit/rollback.
        source_id: The durable source identity (``str(source_id)``); the run's
            scope.
        extractor_version: The NER extractor's version; part of the derivation
            key and recorded as a version stamp.
        materializer_version: The deterministic post-NER materialization logic's version;
            part of the derivation key and recorded as a version stamp.
        input_policy: The raw-vs-contextualized input toggle; part of the
            derivation key and recorded as a version stamp.
        upstream_derivation_key: The consumed upstream run's derivation key (the
            source's active contextualization run's key when ``input_policy`` is
            contextualized, else its active chunking run's key); part of the
            derivation key.

    Returns:
        The reused active :class:`~aizk.pipeline.run.PipelineRun`, or the
        newly-activated one.

    Raises:
        ValueError: If ``input_policy`` is not one of :data:`InputPolicy`'s legal
            values.
    """
    if input_policy not in _VALID_INPUT_POLICIES:
        raise ValueError(
            f"input_policy {input_policy!r} is not one of {sorted(_VALID_INPUT_POLICIES)}; "
            "a typo'd policy must not silently mint a novel derivation key"
        )
    derivation_key = extraction_derivation_key(
        extractor_version=extractor_version,
        materializer_version=materializer_version,
        input_policy=input_policy,
        upstream_derivation_key=upstream_derivation_key,
    )
    version_stamps = {
        "extractor_version": extractor_version,
        "materializer_version": materializer_version,
        "input_policy": input_policy,
    }
    return reuse_or_record_run(
        session,
        stage=MENTION_EXTRACTION_STAGE,
        scope_id=source_id,
        derivation_key=derivation_key,
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


def _contextualized_locator(draft: "MentionDraft") -> tuple[str, int, int]:
    """Return a contextualized draft's ``(chunk_id, contextualization_run_id, context_version)`` locator."""
    assert draft.contextualization_run_id is not None  # noqa: S101 — validator-enforced
    assert draft.context_version is not None  # noqa: S101 — validator-enforced
    return (draft.chunk_id, draft.contextualization_run_id, draft.context_version)


def _consumed_text(draft: "MentionDraft", chunk: Chunk, variant: ContextualizedChunk | None) -> str:
    """Return the text a draft's ``input_span`` indexes into.

    Raw drafts consume the chunk's raw text; contextualized drafts consume the
    resolved variant's non-empty ``contextualized_text``. A present-empty variant
    never reaches here: :func:`_validate_provenance` rejects a contextualized
    draft whose variant is present-empty before span validation runs.
    """
    if draft.input_kind == INPUT_KIND_RAW:
        return chunk.text
    assert variant is not None  # noqa: S101 — resolved by the caller before this is invoked
    return variant.contextualized_text


def _validate_provenance(session: "Session", *, run: PipelineRun, mentions: "Sequence[MentionDraft]") -> None:
    """Validate a batch of drafts' provenance against authoritative rows, before any write.

    Runs the full checklist for the whole batch and raises on the first failing
    check, naming every offending draft/value; a later check never runs once an
    earlier one has failed. Each distinct ``chunk_id``'s :class:`Chunk` row and
    each distinct contextualized locator's :class:`ContextualizedChunk` row is
    loaded at most once for the whole batch.

    Raises:
        ValueError: If ``run`` is not an active mention-extraction run; if any
            draft's ``chunk_id`` does not resolve to a persisted :class:`Chunk`;
            if a resolved chunk's ``source_id`` does not equal ``run.scope_id``;
            if any draft has ``input_kind = raw`` but is not source-anchored; if a
            contextualized draft's locator does not resolve to a persisted
            :class:`ContextualizedChunk`, or resolves to a present-empty variant
            (``contextualized_text = ''`` — the already-self-contained case, where
            extraction reads the raw chunk text and must record the mention with
            raw ``input_kind`` and the chunk as its ``input_ref``); if a
            source-anchored draft's ``source_span`` is out of bounds, inverted, or
            does not slice the raw chunk text to both ``source_anchor_text`` and
            ``surface_form``; or if any draft's ``input_span`` is out of bounds or
            does not slice the consumed input text to ``surface_form``.
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

    # ``run.scope_id`` is the string form of the source identity and the chunk's is a
    # UUID, so the comparison converts; without it every chunk would read as foreign.
    cross_source = sorted({d.chunk_id for d in mentions if str(chunks_by_id[d.chunk_id].source_id) != run.scope_id})
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
    variants_by_locator: dict[tuple[str, int, int], ContextualizedChunk] = {}
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
    bad_source_spans: list[tuple[str, int | None, int | None]] = []
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

    bad_input_spans: list[tuple[str, int, int]] = []
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
    """Persist a batch of mention drafts under ``run``, append-only and idempotent.

    Validates the entire batch against authoritative rows before any write (see
    :func:`_validate_provenance` for the full checklist: run congruence, chunk
    resolution, per-source scoping, the raw-input-implies-source-anchored rule,
    contextualized-locator resolution, and source/input span integrity). Anchor-class
    field congruence is already enforced by :class:`MentionDraft`'s own validators.

    For a contextualized draft, the consumed input text is the resolved
    :class:`~aizk.graph.datamodel.ContextualizedChunk`'s non-empty
    ``contextualized_text``. A locator resolving to a present-empty variant
    (``contextualized_text = ''``, the already-self-contained case) is rejected:
    extraction reads the raw chunk text in that case, so a conformant extractor
    records the mention with raw ``input_kind`` and the chunk as its ``input_ref``.

    For each draft, resolves an existing row in ``run.id`` by its per-anchor-class
    identity tuple (source: ``(run_id, chunk_id, source_span_start,
    source_span_end, surface_form)``; revision: ``(run_id, chunk_id,
    surface_form)``) and reuses it unchanged if present; otherwise inserts a new
    row with a minted ``str(uuid4())`` ``mention_id``, the serialized
    canonical-JSON ``input_ref``, and — for source-anchored rows —
    ``source_occurrence_key`` derived via
    :func:`derive_source_occurrence_key`. A persisted mention row is never updated
    or deleted. Resolution is race-safe under the project's single serialized
    writer (one writer transaction at a time), so no additional locking is
    needed here. Within one call, a class tuple already resolved (reused from the
    store or newly inserted) is tracked in an in-memory dict, so a later draft
    sharing that tuple resolves to the same pending row without a redundant store
    lookup or a flush per draft; the whole batch is flushed exactly once, before
    co-occurrence linking.

    Co-occurrence linking: after resolving/inserting every draft, for each
    distinct ``(run, chunk)`` touched by this call, records one
    :class:`~aizk.graph.datamodel.MentionCooccurrence` row per unordered pair
    drawn from **all** of that chunk's mentions in the run — including mentions
    persisted by an earlier call — so a retry or a second batch touching the same
    chunk converges to the complete pair set rather than only the pairs newly
    introduced. Endpoints belong to the same run and chunk by construction, since
    the pairs are computed from the chunk's own run-scoped mention set; no
    redundant runtime check is needed. Pairs are ordered canonically by Python
    string comparison of the two ``mention_id``s (ASCII hex UUIDs — byte order
    consistent with SQLite's and Postgres's default ``TEXT`` collation) and
    inserted as one multi-row ``ON CONFLICT DO NOTHING`` statement per chunk (on
    the composite primary key), so a retried chunk cannot duplicate or disorder a
    link and a chunk's pairs cost one round trip rather than one per pair.

    Does **not** commit; the caller owns the surrounding transaction (mirroring
    :func:`aizk.graph.persistence.persist_chunks`).

    Args:
        session: Active session; the caller owns commit/rollback.
        run: The extraction run the drafts belong to (see
            :func:`open_extraction_run`).
        mentions: The mention drafts to persist.

    Returns:
        The persisted :class:`~aizk.graph.datamodel.Mention` rows for the supplied
        drafts, in draft order — a reused draft's row is returned as stored (the
        authoritative state), not re-derived from the draft. Drafts within one
        batch that share a class-identity tuple resolve to the same stored row,
        so the returned list carries aliased entries for such duplicates.

    Raises:
        ValueError: See :func:`_validate_provenance`.
    """
    _validate_provenance(session, run=run, mentions=mentions)

    assert run.id is not None  # noqa: S101 — a persisted run always carries an id

    persisted: list[Mention] = []
    touched_chunks: set[str] = set()
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
                mention_id=str(uuid4()),
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


def _link_chunk_cooccurrences(session: "Session", *, run_id: int, chunk_id: str) -> None:
    """Record every unordered co-occurrence pair among a chunk's mentions in ``run_id``.

    Reads **all** of the chunk's mentions in the run (not only those newly
    inserted by the current call), so a retry or a second batch touching the same
    chunk converges to the complete pair set. Pairs are ordered canonically by
    Python string comparison of the two ``mention_id``s — the ASCII hex UUIDs sort
    consistently under SQLite's and Postgres's default ``TEXT`` collation — and
    inserted as one multi-row ``ON CONFLICT DO NOTHING`` statement on the
    composite primary key ``(run_id, mention_id_lo, mention_id_hi)`` (conflict
    resolution applies per row, so this is idempotent exactly like the
    single-pair form it replaces), matching the memo's insert-or-ignore
    idempotency pattern (:func:`aizk.graph.persistence.memo_upsert_and_read`) at
    one round trip per chunk instead of one per pair. Endpoints belong to the
    same run and chunk by construction: the pairs are computed from the chunk's
    own run-scoped mention set.
    """
    chunk_mention_ids = list(
        session.exec(select(Mention.mention_id).where(Mention.run_id == run_id, Mention.chunk_id == chunk_id)).all()
    )
    if len(chunk_mention_ids) < 2:
        return

    pairs: list[dict[str, "int | str"]] = []
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
