# 007 - Indexing, Search, and Retrieval

## Status

2025-12-27

Accepted

## Context

- We need higher recall and robustness for multi-paragraph notes and semi-structured docs than single-vector dense retrieval has delivered.
- Pure lexical (BM25) and exact-match filters miss paraphrases; dense single-vector indexes over-match when documents are long, leading to false positives.
- Learned sparse retrieval (e.g., SPLADE) improves lexical weighting but still struggles with compositional queries and long-context nuance.
- We want a self-hosted, Python-first stack that can run on CPU with a clear upgrade path to GPU.
- The work doubles as an educational experiment in late-interaction retrieval.

## Decision

### Selected Approach

Adopt ColBERT-style late-interaction retrieval using PyLate, defaulting to the Voyager (HNSW) index and optionally FastPLAID for compressed multi-vector storage. Documents will be chunked, encoded into token-level embeddings, indexed with multi-vector support, and queried via MaxSim scoring.

### Rationale

- Late-interaction token matching (MaxSim over per-token vectors) consistently outperforms single-vector dense retrieval on long passages by reducing false positives from over-compressed document embeddings [lancedb.com](https://lancedb.com/blog/late-interaction-efficient-multi-modal-retrievers-need-more-than-just-a-vector-index/) and approaches cross-encoder fidelity at much lower latency [weaviate.io](https://weaviate.io/blog/late-interaction-overview).
- PyLate ships batteries-included indexes: Voyager (HNSW ANN) for straightforward CPU use and PLAID for PQ-compressed multi-vector search, keeping everything in-process and open source [github.com/lightonai/pylate](https://github.com/lightonai/pylate) [github.com/lightonai/fast-plaid](https://github.com/lightonai/fast-plaid).
- Educational value: exercising late-interaction indexing, MaxSim scoring, and compression trade-offs informs future reranking and hybrid pipelines.
- Compared with:
  - Text search + bloom filter: extremely fast but purely lexical; no semantic recall and brittle to paraphrase.
  - BM25: strong for exact terms but under-recovers paraphrases; no semantic soft-match, so recall drops on abstractive queries.
  - Standard dense vectors: good semantics but collapse long docs into one vector, leading to topic drift and false positives; late interaction keeps token specificity.
  - SPLADE: sparse lexical weighting improves recall but still anchored to token overlap; ColBERT captures semantic similarity without requiring shared surface forms.

### Consequences

#### Positive Impacts

- Higher recall and precision on long-form and compositional queries via token-level matching.
- Self-hosted, Python-native stack with CPU-first deploy; optional PLAID compression reduces memory.
- Clear path to hybrid retrieval (e.g., combine BM25 first-stage with ColBERT rerank) if needed.

#### Potential Risks

- Index size grows with tokens; storage and memory heavier than single-vector or sparse indexes.
- Query latency higher than single-vector dense/BM25, especially without PLAID compression.
- Model and index choices may drift; requires monitoring for quality regressions.

#### Mitigation Strategies

- Start with Voyager on a constrained corpus; measure QPS/latency and memory, then enable PLAID compression if needed.
- Keep a BM25 baseline for fallback and for two-stage retrieval if latency climbs.
- Add regression evals on a held-out question set; track recall@k and MRR for drift.

### Alternative Considered

#### Option 1: Text search + bloom filter

- Pros: minimal infra, very fast exact-match filtering.
- Cons: no semantic recall, brittle to paraphrase; unsuitable for long-form notes.
- Reason rejected: fails core recall requirement.

#### Option 2: BM25

- Pros: proven, cheap, strong for exact-term queries.
- Cons: misses paraphrases, weak on compositional semantic queries.
- Reason rejected: recall gap on paraphrased/abstractive questions.

#### Option 3: Standard dense vector retrieval

- Pros: good semantic recall, compact indexes.
- Cons: single-vector collapse causes false positives on long documents; loses token-level specificity.
- Reason rejected: poorer precision/recall on long passages versus late interaction.

#### Option 4: SPLADE (sparse learned retrieval)

- Pros: strong lexical weighting, smaller index than ColBERT, good interpretability.
- Cons: relies on token overlap; weaker on semantic paraphrase than ColBERT; still misses compositional meaning.
- Reason rejected: performance gap on long-context semantic queries; ColBERT better matches our targets per recent late-interaction evaluations [weaviate.io](https://weaviate.io/blog/late-interaction-overview).

## Implementation Details

- Model: PyLate ColBERT checkpoint (e.g., answerai-colbert-small-v1) for token-level embeddings.
- Index: start with PyLate Voyager (HNSW) on CPU; enable PLAID compression if memory exceeds target.
- Pipeline: chunk documents per ADR-005 guidance; encode to token embeddings; store doc-id + chunk-id; serve MaxSim search through PyLate retriever API.
- Evaluation: maintain BM25 + dense single-vector baselines; run recall@k/MRR on a curated question set.
- Deployment: Python service only (no external DB); keep index on disk with periodic rebuilds.

## Related ADRs

- [002 - Content Parsing](./002-content-parsing.md)
- [003 - Database](./003-database.md)
- [004 - Model Provider (Framework)](./004-model-provider.md)
- [005 - Chunking](./005-chunking.md)

<!-- Reference numbers of related decisions
Links to dependent or impacted architectural decisions -->

## Additional Notes

<!-- Any supplementary information
References to documentation
Contact person for further questions -->

- Late interaction overview and MaxSim benefits: [Late Interaction & Efficient Multi-modal Retrievers Need More Than a Vector Index](https://lancedb.com/blog/late-interaction-efficient-multi-modal-retrievers-need-more-than-just-a-vector-index/)
- Late-interaction vs dense and sparse trade-offs: [An Overview of Late Interaction Retrieval Models](https://weaviate.io/blog/late-interaction-overview)
- PyLate ColBERT + Voyager/PLAID implementations: [PyLate](https://github.com/lightonai/pylate), [FastPLAID](https://github.com/lightonai/fast-plaid)
