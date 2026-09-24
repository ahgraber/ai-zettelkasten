"""Summarize documents and contextualize chunks as two document-scoped runs.

This module owns the contextualization domain logic: one LLM pass per document
produces a :class:`~aizk.graph.datamodel.DocumentSummary` under a **summary run**,
and one LLM pass per chunk produces a :class:`~aizk.graph.datamodel.ContextualizedChunk`
**revision** under a separate **variant run**. Both runs are scoped to the
durable source identity and recorded on the shared
:func:`~aizk.pipeline.run.record_run` primitive; each supersedes independently:

- the summary run is keyed over the source markdown's content fingerprint,
  ``summary_version``, the summary prompt hash, and the resolved model
  configuration — a change to any opens a new run, unchanged inputs reuse the
  active run and its summary;
- the variant run is keyed over the summary text's content fingerprint, the
  ordered chunk-set content keys, the ``splitter_version`` of the chunking
  generation read, 2p/1n context-window policy, contextualization prompt hash,
  resolved model configuration, and ``context_version`` — a change to any opens
  a new run, unchanged inputs reuse the active run and its variants.

Splitting summary and variant into two runs is required so a standalone
``context_version`` bump regenerates variants without producing a duplicate
summary under unchanged summary inputs.

Every model invocation passes through the single injected
:class:`~aizk.ai.providers.llm.LLMClient` access point, so a deterministic substitute
drives the runs in tests with no change to this logic. Contextualization is a
**dereferencing revision**: the model rewrites the working chunk into a
self-contained passage with every cross-reference resolved inline, and that
revised text is stored as the variant. The raw chunk row is never modified and
remains the cited, source-faithful unit; the revision is a separate, derived
artifact the extraction stage reads. A chunk the model judges already
self-contained stores an empty revision, signalling "consume the raw chunk
unchanged". :func:`resolve_chunk_text` selects raw vs revised text at use time,
honoring the contextualization on/off toggle and recording which input was used.

Calling convention mirrors the runtime helpers: the persist-phase functions
(:func:`summarize_document`, :func:`contextualize_chunks`) ``add``/``flush`` on the
caller's session and never commit. The **generation-phase** resolvers
(:func:`resolve_summary_text`, :func:`resolve_revisions`) are the exception: they
take the ``engine`` and perform autonomous memo writes in their own short
transactions to checkpoint validated model output, so a retry of a
partially-completed attempt re-invokes the model only for outputs not already
retained. They never hold a transaction across a model call, and the memo is
internal scratch state (:class:`~aizk.graph.datamodel.ContextualizationOutputMemo`),
never read as product state; :func:`consumed_output_memo_keys` enumerates the keys
the persist phase prunes once they are redundant with the committed records.
"""

from aizk.contextualization.core.applicability import (
    record_contextualization_run_applicability,
    record_summary_run_applicability,
)
from aizk.contextualization.core.keys import (
    CONTEXT_PROMPT_DATA_POLICY,
    CONTEXT_PROMPT_HASH,
    CONTEXT_PROMPT_KIND,
    CONTEXT_WINDOW_POLICY,
    CONTEXTUALIZATION_INSTRUCTIONS,
    MAX_REVISION_CHUNK_RATIO,
    MAX_SUMMARY_CHARS,
    MIN_REVISION_LIMIT_CHARS,
    PROMPT_DATA_ENVELOPE,
    SUMMARY_INSTRUCTIONS,
    SUMMARY_LENGTH_GUIDANCE_TEMPLATE,
    SUMMARY_PROMPT_DATA_POLICY,
    SUMMARY_PROMPT_HASH,
    SUMMARY_PROMPT_KIND,
    SUMMARY_SOFT_LIMIT_CHARS,
    SUMMARY_SOFT_LIMIT_RATIO,
    _stable_hash,
    _stable_payload_hash,
    consumed_output_memo_keys,
    contextualization_config_fingerprint,
    contextualization_row_derivation_key,
    contextualization_row_input_fingerprint,
    contextualization_run_derivation_key,
    contextualization_run_input_fingerprint,
    summary_config_fingerprint,
    summary_derivation_key,
    summary_input_fingerprint,
)
from aizk.contextualization.core.revisions import (
    assert_variant_matches_run_input,
    contextualize_chunks,
    generate_revisions,
    resolve_revisions,
)
from aizk.contextualization.core.summary import (
    generate_summary_text,
    resolve_summary_text,
    summarize_document,
)
from aizk.graph.variants import (
    SUMMARY_STAGE,
    VARIANT_STAGE,
    ContextSource,
    ResolvedChunkText,
    StalePlanError,
    _active_run,
    resolve_chunk_text,
)

__all__ = [
    "CONTEXTUALIZATION_INSTRUCTIONS",
    "CONTEXT_PROMPT_DATA_POLICY",
    "CONTEXT_PROMPT_HASH",
    "CONTEXT_PROMPT_KIND",
    "CONTEXT_WINDOW_POLICY",
    "ContextSource",
    "MAX_REVISION_CHUNK_RATIO",
    "MAX_SUMMARY_CHARS",
    "MIN_REVISION_LIMIT_CHARS",
    "PROMPT_DATA_ENVELOPE",
    "ResolvedChunkText",
    "SUMMARY_INSTRUCTIONS",
    "SUMMARY_LENGTH_GUIDANCE_TEMPLATE",
    "SUMMARY_PROMPT_DATA_POLICY",
    "SUMMARY_PROMPT_HASH",
    "SUMMARY_PROMPT_KIND",
    "SUMMARY_SOFT_LIMIT_CHARS",
    "SUMMARY_SOFT_LIMIT_RATIO",
    "SUMMARY_STAGE",
    "StalePlanError",
    "VARIANT_STAGE",
    "assert_variant_matches_run_input",
    "consumed_output_memo_keys",
    "contextualization_config_fingerprint",
    "contextualization_row_derivation_key",
    "contextualization_row_input_fingerprint",
    "contextualization_run_derivation_key",
    "contextualization_run_input_fingerprint",
    "contextualize_chunks",
    "generate_revisions",
    "generate_summary_text",
    "record_contextualization_run_applicability",
    "record_summary_run_applicability",
    "resolve_chunk_text",
    "resolve_revisions",
    "resolve_summary_text",
    "summarize_document",
    "summary_config_fingerprint",
    "summary_derivation_key",
    "summary_input_fingerprint",
]
