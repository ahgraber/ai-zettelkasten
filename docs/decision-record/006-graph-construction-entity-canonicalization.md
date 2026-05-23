# 006 - Graph Construction and Entity Canonicalization

## Status

21 May 2026 - Proposed

## Context

ADR-005 defines chunking as a deterministic, source-faithful stage that emits structural chunks.
Those chunks are suitable for citation and retrieval, but they are not yet a knowledge graph: they do not identify repeated entity mentions, canonical entities, or graph edges between ideas.

The graph stage needs to support a personal AI Zettelkasten: source-faithful research over archived documents, with graph-aware retrieval and future contextualization.
The system should connect ideas without rewriting source text into LLM-generated claims as the primary artifact.
It should also avoid committing to a prespecified upper ontology, external knowledge base, RDF store, or OWL-style inference before there is a concrete interoperability requirement.

The hard part is entity canonicalization without a fixed ontology.
If the system treats extraction-time entity IDs as ground truth, then an early bad merge becomes expensive to repair because dependent edges and claims have already been attached to the wrong identity.
The design therefore needs replayable evidence, versioned derived state, and an explicit repair path before the implementation spends money on extraction, embedding, and graph analysis.

This ADR distills the research draft at [data/graph_research/graph-construction-entity-canonicalization-draft.md](../../data/graph_research/graph-construction-entity-canonicalization-draft.md).
The draft remains the research artifact and mechanism catalogue; this ADR records the architectural decision and validation gates.

## Decision

### Selected Approach

Build graph construction around an **append-only mention store** and a **versioned canonical entity clustering** derived from that store.

The graph stage has two graph surfaces:

- **Document-structure graph.**
  Derive `parent_of` and `precedes` relationships from chunk metadata (`heading_path`, `ordinal`) produced by ADR-005.
  This graph is deterministic and may be materialized when needed.
- **Concept/entity graph.**
  Extract entity mentions from contextualized chunks, assign mentions to canonical entities, and form co-occurrence edges between entities mentioned in the same chunk.

Canonicalization uses these contracts:

- **Mentions are ground truth.**
  Each mention records its surface form, normalized aliases/blocking keys, source `chunk_id`, source span or offset, co-occurring mention IDs, extractor/model versions, and assignment evidence.
  No context embedding is stored; any embedding the resolver needs is recomputed on demand from `(chunk_id, span)`, so an encoder change strands no persisted vectors.
  Mentions are append-only.
- **Canonical entities are derived state.**
  A canonical entity is a versioned cluster of mention IDs.
  The canonical entity set carries a `canon_version` that changes whenever the clustering changes.
- **Entity IDs are lineage handles, not membership-derived identities.**
  An `entity_id` is immutable for the lifetime of that entity record and is never reused.
  A split or merge retires the old ID and mints successor IDs.
  Retired IDs remain permanently resolvable through lineage events.
- **Lineage is append-only.**
  Splits and merges are recorded as lineage events with predecessor IDs, successor IDs, `canon_version`, and the mention-to-successor assignment needed for replay.
- **Claims and edges retain source mention provenance.**
  Any claim, edge, or derived graph artifact that references an entity must retain the mention IDs or equivalent source provenance that produced it.
  Without that provenance, a future split cannot route the artifact to a successor entity soundly.
- **Propagation is deferred, not free.**
  A split decision can be local to one entity cluster, but dependent edge repair, embedding invalidation, cache invalidation, and materialized graph updates are deferred work tracked by dirty sets and background repair.

The first implementation increment is **create-vs-assign**, not graph-topology split detection.
At ingest time the resolver decides whether a mention starts a new entity or joins an existing one using:

- normalized exact-match aliases,
- redundant blocking keys (normalized text, phonetic keys, acronyms, and token shingles),
- MinHash/LSH or equivalent candidate generation where useful,
- an off-the-shelf context-embedding similarity score, recomputed on demand and used only to re-rank lexical candidates,
- conservative assign/spawn thresholds,
- assignment provenance and a review queue for low-margin cases.

Lexical signals carry the decision; the embedding is a subordinate tie-breaker whose value is measured with and without it on the gold set before it is relied on.
The mention-context embedding uses a general off-the-shelf encoder over a marked context window; a released entity-linking encoder is a later, gated upgrade.
It is recomputed per decision from `(chunk_id, span)`, never precomputed or stored, so an encoder change needs no re-embed pass.
The research record's §5a carries the representation rationale, including why marking the mention — not the span-vs-chunk choice — is the variable that matters.

Topology-based split detection is explicitly second-generation.
Ego-splitting, word-sense-induction style co-occurrence clustering, local-PPR conductance, structural-role signals, and hub-sink retrieval signals may nominate or ratify split candidates only after the corpus has enough repeated mentions and graph density.
No graph signal may commit a split by itself.

Before topology-based split machinery is implemented, the project must pass a validation gate:

- a small gold set of mention spans, identity clusters, aliases, hard negatives, and extraction-artifact cases;
- extraction evaluation for span-level NER, coreference quality, co-occurrence edge precision, and error taxonomy;
- offline create-vs-assign benchmarking across blocking, aliasing, MinHash/LSH, and embedding signals;
- cold-start graph viability metrics, including singleton rate, cluster-size distribution, entity degree, ego-net density, and neighbor overlap;
- threshold calibration for split nominators against true polysemy, multifaceted single entities, hubs, and extraction artifacts.

### Rationale

The core guarantee we need is repairability.
An append-only mention store keeps the evidence needed to rebuild or locally re-cut canonical entities when thresholds, extractors, embeddings, or corpus contents change.
Versioned derived clusters make canonicalization replayable and auditable instead of irreversible.

The design follows the same pattern as ADR-005: source-faithful substrate first, model-derived enrichment second, deterministic/versioned gate at the boundary.
Chunking keeps source text stable; graph construction keeps mention evidence stable.
`canon_version` plays the same role for entity clustering that `splitter_version` plays for chunking.

Incremental ER theory supports a narrow version of this design.
For a fixed comparator, tightening a threshold can be treated as a refinement and loosening can be treated as a merge-biased change.
That monotonicity does not apply to embedding-model or NER-model swaps, which can reorder pairwise similarities.
Model swaps are therefore non-monotone re-evaluations, not split-only or merge-only refinements.

Production graph-memory systems such as Graphiti/Zep support the practical first increment: deterministic-first entity deduplication with exact normalization, MinHash/LSH candidate generation, and optional LLM fallback.
They do not prove reversible split lineage, so this ADR uses them only as precedent for create-vs-assign heuristics.

Graph-theoretic methods are useful but not authoritative.
Ego-splitting and word-sense-induction methods can surface likely conflated neighborhoods, and local-PPR conductance can test for structural bottlenecks.
Those methods detect graph personas or communities, not real-world referential identity.
They become evidence only when paired with content, source, name-set, temporal, or embedding evidence.

### Consequences

#### Positive Impacts

- Early extraction mistakes remain repairable because raw mentions are never discarded.
- Canonicalization changes are replayable through `canon_version` and lineage events.
- Dependent embeddings, claims, and edges can be invalidated from explicit ID churn rather than inferred from ad hoc state.
- The MVP can start with cheap deterministic candidate generation and measured thresholds instead of expensive graph machinery.
- The design preserves the project's minimal-infrastructure stance: no triplestore, external KB, or upper ontology is required for the initial graph.

#### Potential Risks

- Correct canonicalization is not guaranteed by the architecture.
  It depends on extraction quality, candidate recall, thresholds, and validation data.
- Blocking misses create a recall floor: a true duplicate that never enters the candidate set may become a duplicate entity.
- Extraction artifacts can become graph structure.
  Bad NER spans, coreference errors, and nearby-entity mistakes may create topology that looks meaningful.
- Personal-corpus cold start may leave too many singleton or low-degree entities for topology-based split signals to work.
- Multifaceted single entities may look like multiple graph personas and be over-split.
- Lazy relink is unsound if claims or edges do not retain source mention provenance.

#### Mitigation Strategies

- Treat the validation gate as a prerequisite for topology-based split work.
- Use redundant blocking plus dense/vector candidate generation to reduce candidate-recall failures.
- Store assignment evidence and source mention provenance for every derived claim and edge.
- Keep graph signals as nominators or ratifiers only; require independent content or source evidence before committing a split.
- Track model and extractor versions on mentions.
  Route model swaps through explicit re-evaluation from materialized candidates, not monotonic refinement.
- Use dirty sets and debounce windows for recomputation.
  Distinguish local decision cost from deferred propagation cost.
- Keep low-margin create-vs-assign decisions reviewable rather than auto-committed.

### Alternative Considered

#### Option 1: Extraction-Time Canonical IDs as Source of Truth

Assign each extracted mention an entity ID during extraction and treat the resulting entity set as canonical.

- _Pros._
  Simpler initial implementation and fewer moving parts.
- _Cons._
  Early bad merges are hard to undo; dependent claims and edges have no reliable path to successor entities.
- _Reason for not selecting._
  It optimizes the first ingest at the cost of long-term correctness and replayability.

#### Option 2: Prespecified Ontology or Triplestore First

Define entity classes and relation constraints up front, or adopt RDF/SHACL/SPARQL as the primary graph store.

- _Pros._
  Stronger validation for declared functional relations and better interoperability if RDF consumers exist.
- _Cons._
  Requires declaring the schema before the corpus has shown what distinctions matter; adds infrastructure and modeling work not yet justified.
- _Reason for not selecting._
  The project needs an emergent posterior validation gate, not an upstream ontology that extraction must conform to.

#### Option 3: External Identifier Canonicalization First

Use DOI, arXiv, ISBN, ORCID, GitHub, or other external identifiers as the primary canonicalization strategy.

- _Pros._
  High precision where registry-backed IDs exist.
- _Cons._
  Applies mainly to references and people, not concepts; existing scrape data lacks enough author and identifier metadata; backfill would be expensive.
- _Reason for not selecting._
  External IDs are useful later for the reference layer, but they do not solve concept/entity canonicalization across arbitrary archived text.

#### Option 4: Build Split Machinery First

Start with ego-splitting, conductance, WSI, and structural-role methods as the main canonicalization system.

- _Pros._
  Directly targets the hardest failure mode: recovering from conflated entities.
- _Cons._
  Requires a bootstrapped graph with enough repeated mentions and reliable edges.
  In a cold personal corpus, ego-nets are often sparse or tree-like, and graph methods overfit extraction artifacts.
- _Reason for not selecting._
  Create-vs-assign is upstream of every split trigger.
  Split detection is deferred until validation metrics show the graph has enough signal.

#### Option 5: Embedding-as-Edge Query-Induced Graph

Represent nodes and edges primarily as embeddings, then materialize query-specific graphs by thresholding edge similarity at query time.

- _Pros._
  Avoids early relation schema design and offers flexible semantic traversal.
- _Cons._
  Pushes canonicalization into threshold calibration; graph topology changes per query; polysemous relations and embedding hubness become load-bearing.
- _Reason for not selecting._
  A fixed co-occurrence/entity graph is simpler, more auditable, and better aligned with repairable canonicalization.

## Implementation Details

- **Mention store.**
  Persist mention records with source chunk provenance (`chunk_id`, span), surface form, normalized aliases, blocking keys, co-occurring mention IDs, extractor versions, assignment scores, and assignment decision metadata; no context embedding is persisted, since it is recomputed on demand from `(chunk_id, span)`.
- **Entity store.**
  Persist current canonical entities as versioned clusters of mention IDs.
  Entity membership is versioned state; `entity_id` is a lineage handle.
- **Lineage log.**
  Persist append-only split/merge events with predecessor IDs, successor IDs, `canon_version`, and mention routing.
- **Derived artifacts.**
  Claims, relation edges, co-occurrence edges, and retrieval graph artifacts must keep the source mention IDs that produced them.
- **Create-vs-assign MVP.**
  Implement candidate generation, scoring, thresholding, and low-margin review before graph split machinery.
- **Validation data.**
  Build and maintain a small gold set before calibrating thresholds or enabling topology-based split commits.
- **Version discipline.**
  Bump `canon_version` on observable canonical clustering changes.
  Treat NER, coreference, embedding, and resolver behavior changes as versioned inputs to canonicalization.
- **Out of scope for this ADR.**
  RDF/triplestore projection, external registry-backed reference canonicalization, LLM judge adoption, and topology-based automatic split commits.

## Infrastructure Impact

Preserving every stage of the transformation pipeline — original Markdown, document summary, raw chunk, contextualized chunk, and entity mentions — is the price of the repairability guarantee this ADR is built on.
Mentions are append-only and nothing upstream is discarded, so the store grows monotonically and holds several representations of the same source text at once.
That cost lands in two places: total storage, and the write/query profile the database must serve. (The document summary is treated here as a short per-document contextualization input; it is not yet formalized in its own ADR, and the analysis below does not depend on its exact form because it is small relative to chunks, vectors, and mentions.)

### Storage requirements

The pipeline stores the same content several times over, and derived vectors — not text — dominate the footprint.
The estimates below are order-of-magnitude for a personal corpus of ~10K documents at ~10 chunks/document and a 1536-dimension embedding (~6 KB per `float32` vector); they scale linearly with each factor.

| Artifact                                  | What drives its size                                                                                   | Order of magnitude                      |
| ----------------------------------------- | ------------------------------------------------------------------------------------------------------ | --------------------------------------- |
| Original Markdown                         | document text                                                                                          | ~100 MB                                 |
| Document summary                          | one short blurb per document                                                                           | ~10 MB                                  |
| Raw chunks                                | a partition of the document, plus per-chunk metadata (ids, hashes, `heading_path`, `span`, provenance) | ~100 MB text + ~30 MB metadata          |
| Contextualized chunks                     | a short context blurb per chunk                                                                        | ~30 MB as a delta, ~130 MB as full text |
| Chunk embeddings                          | one vector per chunk, plus the HNSW graph                                                              | ~0.6–1 GB                               |
| Mention store                             | ~10 mentions/chunk, each with aliases, blocking keys, co-occurring IDs, scores, versions               | ~0.5–1 GB metadata                      |
| Mention context vectors (if materialized) | one vector per mention                                                                                 | ~6 GB                                   |
| Co-occurrence edges + lineage             | one row per entity pair per chunk; append-only lineage events                                          | a few hundred MB                        |

Two levers move the total by an order of magnitude:

- **Mention context vectors are never materialized — the largest line item, avoided by design.**
  Roughly 1M mentions, each carrying its own vector, would be ~6 GB — larger than every other artifact combined.
  Instead the create-vs-assign embedding is recomputed on demand from `(chunk_id, span)` with an off-the-shelf encoder over a marked window (see Decision and the research record's §5a), and never persisted.
  That keeps the corpus near ~2 GB instead of past ~8 GB, and lets an encoder swap strand nothing.
- **Store the contextualized chunk as a delta, not a copy.**
  The contextualized chunk is the raw chunk plus a short context blurb.
  Persisting only the blurb — and reconstructing the embedded text at embed time — avoids a second full copy of the corpus and removes any risk of the two representations drifting.
  Retrieval still cites the raw chunk, so the source-faithful guarantee holds.

Append-only growth compounds these figures over time.
A `splitter_version` bump (re-chunking) or a `canon_version` bump (re-clustering) supersedes prior artifacts rather than overwriting them.
Without the `chunk_id`-churn cleanup ADR-005 defines, superseded embeddings and mentions accumulate, so plan for periodic compaction of invalidated vectors and an explicit retention policy for retired lineage — otherwise storage grows with revision count, not just corpus size.

### Database performance

Entity extraction inverts the write profile the rest of the pipeline assumes.
Chunking emits ~10 rows per document; extraction over those chunks emits ~10 mentions plus their co-occurrence edges per chunk — roughly two orders of magnitude more rows, written in bursts during ingest and backfill.
Three effects follow:

- **Write throughput against a serialized writer.**
  ADR-003 serializes all SQLite writers and names ~100 sustained writes/sec as a migration trigger, with >500 writes/sec the zone where Litestream's checkpoint-race bugs appear.
  A backfill that extracts entities across the whole corpus is exactly this pattern.
  Batch mention and edge inserts into a few large transactions per chunk or per document rather than row-at-a-time, and run large re-clustering passes as throttled background work, not foreground ingest.
- **Vector count, not dimension, sets the ANN ceiling.**
  ADR-003 sizes `sqlite-vec` for 10^5–10^6 vectors.
  Chunk embeddings (~100K) sit comfortably inside that range; materialized mention vectors (~1M) sit at the top of it, and any growth in corpus size or mentions-per-chunk crosses it.
  This is the retrieval-side pressure point that moves the system to Postgres + pgvector independent of write load.
- **Graph traversal is offline, so it does not stress the OLTP path.**
  The topology analysis this ADR defers — ego-splitting, conductance, ego-net density — is batch work over the co-occurrence edge set, best run in an in-memory graph library (networkx/igraph) loaded from bulk reads rather than as recursive SQL.
  Neither SQLite nor Postgres is a graph database; keeping traversal offline means the store only has to hold edges and serve bulk reads, which both do well.
  Live retrieval needs only 1–2 hop neighbor lookups, which a single indexed edge table serves on either engine.

Background repair and foreground retrieval also share the database.
Deferred propagation — dirty sets, background edge repair, embedding invalidation — writes while users read.
SQLite's WAL mode keeps readers unblocked behind the serialized writer, so retrieval latency holds; the constraint is that repair writes serialize with ingest writes through that one writer.
Postgres's MVCC removes that serialization when repair and ingest must run concurrently at volume.

### SQLite vs. Postgres for the graph stage

The graph stage does not change ADR-003's default — SQLite + `sqlite-vec`, with the schema kept portable to Postgres + pgvector — but it makes three of that ADR's migration triggers more likely to fire and adds one of its own:

- **Sustained write throughput** from corpus-wide entity backfill is the first trigger likely to hit, well before multi-node deployment.
- **Vector count** crosses `sqlite-vec`'s comfort zone sooner if per-mention vectors are materialized — a reason to prefer the reference/pool approach above, or to gate materialization on a Postgres move.
- **Concurrent repair plus ingest at volume** favors MVCC; until then, WAL with a serialized writer is adequate because retrieval reads are never blocked.

The graph schema (mention store, entity clusters, lineage log, co-occurrence edges) is ordinary relational data and maps to Postgres with the minimal-change path ADR-003 already documents; the vector tables map `sqlite-vec` → pgvector HNSW.
Keeping mention vectors optional and edges in a flat, index-friendly table preserves that path.
The recommendation is to stay on SQLite through the MVP and the create-vs-assign increment, then revisit when backfill write rate, materialized-vector count, or concurrent repair load crosses an ADR-003 threshold — whichever comes first — rather than at a fixed corpus size.

## Related ADRs

- [002 - Content Parsing](./002-content-parsing.md)
- [003 - Database](./003-database.md)
- [004 - Model Provider](./004-model-provider.md)
- [005 - Chunking](./005-chunking.md)
- [007 - Embedding](./007-embedding.md)
- [008 - Indexing, Search, and Retrieval](./008-index-search-retrieval.md)

## Additional Notes

Research record:
[Graph Construction and Entity Canonicalization Draft](../../data/graph_research/graph-construction-entity-canonicalization-draft.md)

Key references carried by the research record:

- Whang & Garcia-Molina, "Incremental Entity Resolution on Rules and Data," VLDB J. 2014.
- Epasto, Lattanzi & Paes Leme, "Ego-splitting Framework: from Non-Overlapping to Overlapping Clusters," KDD 2017.
- Ustalov et al., "Watset: Local-Global Graph Clustering with Applications in Sense and Frame Induction," Computational Linguistics 2019.
- Monath et al., "Scalable Hierarchical Clustering with Tree Grafting," KDD 2019.
- Ilyas et al., "Saga: A Platform for Continuous Construction and Serving of Knowledge At Scale," SIGMOD 2022.
- Cai & O'Connor, "Understanding the Effect of Knowledge Graph Extraction Error on Downstream Graph Analyses," 2025.
