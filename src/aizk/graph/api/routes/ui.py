"""The content explorer: the graph operator's chunk browser and paired search.

These HTMX server-rendered routes are mounted on the graph operator app
(``graph/api/main.py``) at ``/ui/explore`` behind its ``TrustedHostMiddleware``
perimeter, and every route resolves the request
:class:`~aizk.conversion.auth.principal.Principal` through the same
:func:`~aizk.conversion.api.dependencies.get_principal` dependency the JSON API
uses, so the explorer is no weaker a perimeter than the API beside it. The routes
are ``include_in_schema=False`` where mounted, keeping HTML out of the OpenAPI.

The document browser's left spine lists the active chunking run's chunks in reading
order with their chunking facts; the detail panel shows the selected chunk's current
contextualized representation (distinct from the raw chunk) with provenance and the
on-demand reconstructed source markdown; the search view renders one paired
raw │ contextualized row per matching chunk. Task monitoring lives in the
descriptor-driven console (:mod:`aizk.console`), not here.
"""

from __future__ import annotations

import html
import importlib.resources
import json
import logging
import re
from typing import TYPE_CHECKING, Annotated, Any
from uuid import UUID

from sqlmodel import Session, select

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.templating import Jinja2Templates

from aizk.conversion.api.dependencies import get_principal
from aizk.conversion.auth import Principal
from aizk.conversion.datamodel.source import Source
from aizk.graph.api.dependencies import get_blob_reader, get_db_session, get_search_provider
from aizk.graph.contextualization import VARIANT_STAGE, ContextSource, resolve_chunk_text
from aizk.graph.datamodel import Chunk, ContextualizedChunk, DocumentSummary
from aizk.graph.persistence import active_chunking_run, document_order_chunks, run_input
from aizk.graph.search import SearchKind
from aizk.pipeline.run import PipelineRun, RunStatus

if TYPE_CHECKING:
    from aizk.graph.markdown_source import BlobReader
    from aizk.graph.search import SearchProvider

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ui/explore", tags=["ui"])

_TEMPLATES = Jinja2Templates(directory=str(importlib.resources.files("aizk.graph") / "templates"))

# --------------------------------------------------------------------------- #
# Explorer — document browser, detail panel, and paired search results.
#
# The document browser's left spine lists the active chunking run's chunks in
# reading (``span_start``) order with their chunking facts; the right detail panel
# shows the selected chunk's *current contextualized representation* (via
# :func:`resolve_chunk_text` over the active variant run's committed
# ``ContextualizedChunk``) distinct from the raw chunk, with provenance and the
# on-demand reconstructed source markdown. The search-results view renders one
# paired raw │ contextualized row per matching chunk, marking the operator's term
# on whichever side(s) matched; selecting a row opens the document browser at that
# chunk with the detail panel populated.
# --------------------------------------------------------------------------- #


def _heading_path(chunk) -> list[str]:  # noqa: ANN001 - Chunk row
    """Decode a chunk row's stored heading path (a JSON array) into a list of strings."""
    return list(json.loads(chunk.heading_path_json))


def _source_title(session: Session, source_id: str) -> str:
    """Return the enriched source title for a ``source_id``, falling back to the id.

    ``source_id`` is the durable source identity (``str(source_id)``); the
    :class:`Source` row keys on the typed ``UUID``, so the id is parsed before the
    lookup. A malformed id or a missing source row falls back to the ``source_id``
    string, mirroring the jobs page's title fallback.
    """
    try:
        source_uuid = UUID(source_id)
    except ValueError:
        return source_id
    source = session.exec(select(Source).where(Source.source_id == source_uuid)).first()
    return (source.title if source is not None else None) or source_id


def _active_variant_run(session: Session, source_id: str) -> PipelineRun | None:
    """Return the source's active contextualized-variant run, or ``None``.

    ``None`` means the source has no committed active variant run — a source still
    mid-contextualization or never contextualized — so the explorer shows raw chunks
    with no contextualized representation.
    """
    return session.exec(
        select(PipelineRun).where(
            PipelineRun.stage == VARIANT_STAGE,
            PipelineRun.scope_id == source_id,
            PipelineRun.status == RunStatus.ACTIVE,
        )
    ).one_or_none()


def _active_variants_by_chunk(session: Session, variant_run: PipelineRun | None) -> dict[str, ContextualizedChunk]:
    """Map ``chunk_id`` → its committed variant in the active variant run.

    Empty when there is no active variant run, so a source mid-contextualization
    contributes no contextualized representation and no retained intermediate output
    (the memo) is ever read here.
    """
    if variant_run is None or variant_run.id is None:
        return {}
    variants = session.exec(select(ContextualizedChunk).where(ContextualizedChunk.run_id == variant_run.id)).all()
    return {variant.chunk_id: variant for variant in variants}


def highlight_terms(content: str, terms: list[str]) -> str:
    """HTML-escape ``content`` and wrap each literal term occurrence in ``<mark>``.

    Content is escaped **first**, so arbitrary document text cannot inject markup;
    only the explorer's own ``<mark>`` tags are then added. Each operator term is
    matched literally (case-insensitive) against the escaped text. Returns markup
    safe to render with autoescaping disabled (the template marks it ``|safe``).

    Args:
        content: Raw chunk or revision text to display.
        terms: The operator's whitespace-split search terms to mark.

    Returns:
        HTML-escaped ``content`` with literal term matches wrapped in ``<mark>``.
    """
    escaped = html.escape(content)
    usable = [term for term in terms if term]
    if not usable:
        return escaped
    pattern = re.compile("|".join(re.escape(html.escape(term)) for term in usable), re.IGNORECASE)
    return pattern.sub(lambda match: f"<mark>{match.group(0)}</mark>", escaped)


def _resolve_representation(raw_text: str, variant: ContextualizedChunk | None) -> tuple[str, bool, bool]:
    """Return the chunk's current contextualized representation and its partition flags.

    Applies :func:`resolve_chunk_text` over the active variant (contextualization
    enabled iff a variant exists). Returns ``(text, has_variant, self_contained)``:
    ``has_variant`` is whether an active variant row exists; ``self_contained`` is
    whether that variant's revision is empty (the run judged no rewrite needed, so
    the raw chunk is the consumed representation).
    """
    contextualized_text = variant.contextualized_text if variant is not None else None
    resolved = resolve_chunk_text(
        raw_text,
        contextualized_text=contextualized_text,
        contextualization_enabled=variant is not None,
    )
    has_variant = variant is not None
    self_contained = has_variant and variant.contextualized_text == ""
    return resolved.text, has_variant, self_contained


def _spine_chunks(
    session: Session, source_id: str
) -> tuple[list[dict[str, Any]], PipelineRun | None, PipelineRun | None]:
    """Build the spine entries for a source's active chunking run, in reading order.

    Lists the active chunking run's chunks ordered by ``span_start`` (document
    order), each with its heading path, span, char count, and self-contained marker
    (true iff its active variant's revision is empty). Returns the spine entries and
    the active chunking and variant runs (either may be ``None``).
    """
    chunking_run = active_chunking_run(session, source_id)
    variant_run = _active_variant_run(session, source_id)
    variants = _active_variants_by_chunk(session, variant_run)
    entries: list[dict[str, Any]] = []
    if chunking_run is not None and chunking_run.id is not None:
        for chunk, span_start, span_end in document_order_chunks(session, chunking_run.id):
            variant = variants.get(chunk.chunk_id)
            entries.append(
                {
                    "chunk_id": chunk.chunk_id,
                    "heading_path": _heading_path(chunk),
                    "span_start": span_start,
                    "span_end": span_end,
                    "char_count": chunk.char_count,
                    "has_variant": variant is not None,
                    "self_contained": variant is not None and variant.contextualized_text == "",
                }
            )
    return entries, chunking_run, variant_run


def _load_detail(
    session: Session,
    blob_reader: "BlobReader",
    *,
    source_id: str,
    chunk_id: str,
    search_terms: list[str],
) -> dict[str, Any] | None:
    """Build the detail-panel context for a chunk, or ``None`` if not in the active run.

    Resolves the chunk's current contextualized representation via
    :func:`resolve_chunk_text` over the active variant run's committed variant
    (revision, or the raw chunk text marked self-contained when the revision is
    empty), distinct from the raw chunk. Surfaces provenance — the producing variant
    run with its ``context_version`` and ``model_profile``, plus lineage to the
    document summary, the chunking generation, and the source markdown — and
    reconstructs the source markdown **on demand** through the injected blob reader;
    a missing conversion output degrades the markdown section rather than failing the
    page. ``search_terms`` are marked on the rendered raw / contextualized text.
    """
    chunking_run = active_chunking_run(session, source_id)
    if chunking_run is None or chunking_run.id is None:
        return None
    ordered = {
        chunk.chunk_id: (chunk, span_start, span_end)
        for chunk, span_start, span_end in document_order_chunks(session, chunking_run.id)
    }
    if chunk_id not in ordered:
        return None
    chunk, span_start, span_end = ordered[chunk_id]

    variant_run = _active_variant_run(session, source_id)
    variant = _active_variants_by_chunk(session, variant_run).get(chunk_id)
    representation, has_variant, self_contained = _resolve_representation(chunk.text, variant)

    provenance: dict[str, Any] = {
        "has_variant": has_variant,
        "self_contained": self_contained,
        "chunking_run_id": chunking_run.id,
    }
    if variant is not None and variant_run is not None:
        stamps = json.loads(variant_run.version_stamps_json)
        summary_run = session.get(PipelineRun, variant.summary_run_id)
        summary = session.exec(select(DocumentSummary).where(DocumentSummary.run_id == variant.summary_run_id)).first()
        provenance.update(
            {
                "variant_run_id": variant_run.id,
                "variant_run_status": variant_run.status.value,
                "context_version": variant.context_version,
                "model_profile": stamps.get("model_profile", ""),
                "summary_run_id": variant.summary_run_id,
                "summary_run_status": summary_run.status.value if summary_run is not None else "",
                "summary_id": summary.id if summary is not None else None,
                "chunking_run_id": variant.chunking_run_id,
            }
        )

    markdown = _load_source_markdown(session, blob_reader, chunking_run.id)

    return {
        "source_id": source_id,
        "chunk_id": chunk_id,
        "heading_path": _heading_path(chunk),
        "span_start": span_start,
        "span_end": span_end,
        "char_count": chunk.char_count,
        "raw_html": highlight_terms(chunk.text, search_terms),
        "representation_html": highlight_terms(representation, search_terms),
        "consumed_source": (ContextSource.CONTEXTUALIZED if has_variant else ContextSource.RAW).value,
        "has_variant": has_variant,
        "self_contained": self_contained,
        "provenance": provenance,
        "markdown": markdown,
    }


def _load_source_markdown(session: Session, blob_reader: "BlobReader", chunking_run_id: int) -> dict[str, Any]:
    """Reconstruct the source markdown on demand, degrading gracefully on a missing output.

    The source markdown's locator is the chunking run's input
    ``conversion_output_id`` (a string; ``S3MarkdownSource.load`` takes the int row
    id). A missing run input or a missing conversion output (the reader raises
    :class:`ValueError`) degrades the markdown section — recording the locator and an
    ``available=False`` flag — rather than returning a 500.
    """
    from aizk.graph.markdown_source import S3MarkdownSource

    inp = run_input(session, chunking_run_id)
    if inp is None:
        return {"available": False, "conversion_output_id": None}
    conversion_output_id = inp.conversion_output_id
    source = S3MarkdownSource(session.get_bind(), blob_reader)
    try:
        loaded = source.load(int(conversion_output_id))
    except (ValueError, OSError) as exc:
        logger.warning("source markdown unavailable for conversion_output=%s: %s", conversion_output_id, exc)
        return {"available": False, "conversion_output_id": conversion_output_id}
    return {
        "available": True,
        "conversion_output_id": conversion_output_id,
        "markdown_hash_xx64": loaded.markdown_hash_xx64,
        "text": loaded.text,
    }


@router.get("/chunks")
def graph_ui_explorer(
    request: Request,
    session: Annotated[Session, Depends(get_db_session)],
    blob_reader: Annotated["BlobReader", Depends(get_blob_reader)],
    _principal: Annotated[Principal, Depends(get_principal)],
    source_id: Annotated[str | None, Query()] = None,
    chunk_id: Annotated[str | None, Query()] = None,
):
    """Render the document browser: the active chunking run's spine and a detail panel.

    The left spine lists the active chunking run's chunks ordered by ``span_start``
    (heading path, span, char count, self-contained marker); the right detail panel
    shows the selected chunk's current contextualized representation when a
    ``chunk_id`` is given. A full page on a normal load, or the inner partial on an
    ``HX-Request`` so a selection swaps the browser without a reload.
    """
    spine, chunking_run, variant_run = ([], None, None) if not source_id else _spine_chunks(session, source_id)
    title = _source_title(session, source_id) if source_id else ""

    detail = None
    if source_id and chunk_id:
        detail = _load_detail(session, blob_reader, source_id=source_id, chunk_id=chunk_id, search_terms=[])

    context = {
        "source_id": source_id or "",
        "title": title,
        "spine": spine,
        "selected_chunk_id": chunk_id or "",
        "detail": detail,
        "chunking_run_id": chunking_run.id if chunking_run is not None else None,
        "variant_run_id": variant_run.id if variant_run is not None else None,
    }
    template = "explorer_panel.html" if request.headers.get("HX-Request") else "explorer.html"
    return _TEMPLATES.TemplateResponse(request, template, context)


@router.get("/chunks/detail")
def graph_ui_explorer_detail(
    request: Request,
    session: Annotated[Session, Depends(get_db_session)],
    blob_reader: Annotated["BlobReader", Depends(get_blob_reader)],
    _principal: Annotated[Principal, Depends(get_principal)],
    source_id: Annotated[str, Query()],
    chunk_id: Annotated[str, Query()],
):
    """Render the detail panel for one chunk, or 404 if it is not in the active run."""
    detail = _load_detail(session, blob_reader, source_id=source_id, chunk_id=chunk_id, search_terms=[])
    if detail is None:
        raise HTTPException(status_code=404, detail=f"chunk {chunk_id} not in the active run of {source_id}")
    return _TEMPLATES.TemplateResponse(request, "explorer_detail.html", {"detail": detail})


def _parse_search_kind(value: str | None) -> SearchKind:
    """Parse the search type filter into a :class:`SearchKind`, defaulting to ``EITHER``."""
    if not value:
        return SearchKind.EITHER
    try:
        return SearchKind(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_kind", "message": "Invalid search type filter"},
        ) from exc


def _search_rows(
    session: Session,
    provider: "SearchProvider",
    *,
    query: str,
    kind: SearchKind,
) -> list[dict[str, Any]]:
    """Run the search and build one paired raw │ contextualized row per matching chunk.

    For each :class:`~aizk.graph.search.SearchResult`, renders the raw chunk text and
    the chunk's current contextualized representation (via :func:`resolve_chunk_text`
    over the active variant), marking the operator's terms on whichever side(s)
    matched (the per-side flags), so one chunk yields one row regardless of how many
    sides it matched.
    """
    terms = query.split()
    rows: list[dict[str, Any]] = []
    titles: dict[str, str] = {}
    variant_caches: dict[str, dict[str, ContextualizedChunk]] = {}
    for result in provider.search(query, kind):
        chunk = session.get(Chunk, result.chunk_id)
        if chunk is None:  # pragma: no cover - an indexed chunk_id always has a row
            continue
        if result.source_id not in titles:
            titles[result.source_id] = _source_title(session, result.source_id)
        if result.source_id not in variant_caches:
            variant_caches[result.source_id] = _active_variants_by_chunk(
                session, _active_variant_run(session, result.source_id)
            )
        variant = variant_caches[result.source_id].get(result.chunk_id)
        representation, _has_variant, self_contained = _resolve_representation(chunk.text, variant)
        rows.append(
            {
                "source_id": result.source_id,
                "chunk_id": result.chunk_id,
                "title": titles[result.source_id],
                "heading_path": _heading_path(chunk),
                "self_contained": self_contained,
                "matched_in_chunk": result.matched_in_chunk,
                "matched_in_contextualized": result.matched_in_contextualized,
                # Mark the term only on the side(s) the provider flagged as matched.
                "raw_html": highlight_terms(chunk.text, terms) if result.matched_in_chunk else html.escape(chunk.text),
                "representation_html": (
                    highlight_terms(representation, terms)
                    if result.matched_in_contextualized
                    else html.escape(representation)
                ),
            }
        )
    return rows


@router.post("/chunks/search")
def graph_ui_explorer_search(
    request: Request,
    session: Annotated[Session, Depends(get_db_session)],
    provider: Annotated["SearchProvider", Depends(get_search_provider)],
    _principal: Annotated[Principal, Depends(get_principal)],
    query: Annotated[str, Form(max_length=200)] = "",
    kind: Annotated[str | None, Form()] = None,
):
    """Render the paired search-results partial for an operator query.

    Posts the operator's literal query (and optional ``chunk``/``contextualized``/
    ``either`` type filter) and renders one paired raw │ contextualized row per
    matching chunk, with the term marked on whichever side(s) matched. An empty or
    whitespace-only query yields an empty result set. Selecting a row opens the
    document browser at that chunk (an ``hx-get`` to the explorer route), with the
    detail panel populated.
    """
    search_kind = _parse_search_kind(kind)
    rows = _search_rows(session, provider, query=query, kind=search_kind)
    document_count = len({row["source_id"] for row in rows})
    context = {"query": query, "kind": search_kind.value, "rows": rows, "document_count": document_count}
    return _TEMPLATES.TemplateResponse(request, "explorer_results.html", context)
