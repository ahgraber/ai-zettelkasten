"""Graph-stage persistence and contextualization for converted Markdown artifacts.

This package sits between the pure :mod:`aizk.chunking` splitter and the (separate)
mention-extraction stage. It persists the splitter's chunks durably and
idempotently, then summarizes each document and contextualizes each chunk so the
corpus is a replayable substrate rather than recomputed-and-discarded output.

The pipeline reads a conversion Markdown artifact, runs ``aizk.chunking.split``
in-process, persists the resulting chunks under a chunking run, and then
produces a per-document summary and a per-chunk contextualized revision. Chunk
content rows are content-addressed and immutable; invalidation is expressed only
as a run-status transition on the shared ``pipeline_runs`` primitive, never as a
row mutation.

Storage and the run / dataset-version primitive are shared with the conversion
stage: graph tables live in the conversion SQLite database and the run record
plus transition-event log come from :mod:`aizk.pipeline`. Importing this package
(via :mod:`aizk.graph.datamodel`) registers the graph tables on the shared
``SQLModel.metadata``.

Process entry points that run this stage set a descriptive ``setproctitle`` at
their own boundary, mirroring :mod:`aizk.conversion`.
"""

from __future__ import annotations

from aizk.graph._version import CONTEXT_VERSION, SUMMARY_VERSION
from aizk.graph.contextualization import (
    SUMMARY_STAGE,
    VARIANT_STAGE,
    ContextSource,
    ResolvedChunkText,
    contextualize_chunks,
    resolve_chunk_text,
    summarize_document,
)
from aizk.graph.datamodel import (
    Chunk,
    ChunkRunInput,
    ChunkRunManifest,
    ContextualizationJob,
    ContextualizedChunk,
    DocumentSummary,
)
from aizk.graph.handler import ContextualizationStageHandler
from aizk.graph.llm import LLMClient, PydanticAILLMClient, StubLLMClient
from aizk.graph.persistence import (
    CHUNKING_STAGE,
    active_chunking_run,
    chunks_of_run,
    current_chunk_ids,
    manifest_of_run,
    members_of_run,
    persist_chunks,
    reconstruct_chunk,
    run_input,
)
from aizk.graph.workunit import (
    LoadedMarkdown,
    MarkdownSource,
    ProcessResult,
    enqueue_backfill,
    enqueue_document,
    process_document,
)

__all__ = [
    "CHUNKING_STAGE",
    "CONTEXT_VERSION",
    "SUMMARY_STAGE",
    "SUMMARY_VERSION",
    "VARIANT_STAGE",
    "Chunk",
    "ChunkRunInput",
    "ChunkRunManifest",
    "ContextSource",
    "ContextualizationJob",
    "ContextualizationStageHandler",
    "ContextualizedChunk",
    "DocumentSummary",
    "LLMClient",
    "LoadedMarkdown",
    "MarkdownSource",
    "ProcessResult",
    "PydanticAILLMClient",
    "ResolvedChunkText",
    "StubLLMClient",
    "active_chunking_run",
    "chunks_of_run",
    "contextualize_chunks",
    "current_chunk_ids",
    "enqueue_backfill",
    "enqueue_document",
    "manifest_of_run",
    "members_of_run",
    "persist_chunks",
    "process_document",
    "reconstruct_chunk",
    "resolve_chunk_text",
    "run_input",
    "summarize_document",
]
