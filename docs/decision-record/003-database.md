# 003 - Database(s)

## Status

21 June 2025 - Proposed
8 October 2025 – Revised

<!-- Proposed/Accepted/Revised/Deprecated/Superseded -->

## Context

The AI Zettelkasten manages a large volume of structured data over multiple transformations (documents, chunks, relationships, metadata, embeddings, etc.). These transformations are computationally expensive, so intermediate and final results must be persisted and efficiently retrievable by both primary-key lookups and vector similarity queries.

Constraints and preferences:

- **Local-first**: should run fully offline on developer laptops and CI with minimal services.
- **Embed-friendly**: embeddable engine preferred over client/server.
- **Simple integration**: leverage SQLModel/SQLAlchemy where possible.
- **Vector-native**: support approximate nearest neighbor (ANN) indexes (e.g., HNSW) and common distances (cosine/L2/IP).
- **Future migration**: ability to move to a networked store later without major model changes.

## Decision

### Selected approach: **SQLite + `sqlite-vec`** (local-first)

Use **SQLite** as the primary local, embedded OLTP store and add **`sqlite-vec`** as a runtime extension to persist and query embedding vectors. `sqlite-vec` is a no-dependency C extension that works anywhere SQLite runs (Linux/macOS/Windows, WASM, mobile) and exposes a simple SQL interface via virtual tables. This keeps the system maximally portable while providing fast, HNSW-backed ANN search that is "good enough" for local development and small/medium datasets.

Note: see [slaily/aiosqlitepool: 🛡️A resilient, high-performance asynchronous connection pool layer for SQLite, designed for efficient and scalable database operations.](https://github.com/slaily/aiosqlitepool)

SQLite UIs:

- [SQLiteViewer](https://alpha.sqliteviewer.app)
- [Beekeeper Studio](https://www.beekeeperstudio.io/)

> Why SQLite over DuckDB for the default? SQLite is a general-purpose, transactional store with battle-tested concurrency semantics for application state. DuckDB excels at OLAP and columnar analytics; we still use DuckDB for ad‑hoc analysis, but SQLite fits day‑to‑day app storage, migrations, and packaging better. With `sqlite-vec`, SQLite now covers vector needs as well.

### Alternatives and when to choose them

1. **[DuckDB](https://duckdb.org/) + [VSS extension](https://duckdb.org/docs/stable/core_extensions/vss.html)** (analytics‑heavy or columnar workloads)

   Choose when you primarily run analytical queries, large scans, or want columnar performance. DuckDB's **VSS** extension provides HNSW indexes over fixed-size list columns and integrates cleanly with array functions, joins, and lateral queries. Keep DuckDB for notebooks and batch pipelines; optionally mirror vectors from SQLite for unified retrieval.

   [Duck UI](https://demo.duckui.com/) is an open-source UI for DuckDB.

2. **[Turso](https://turso.tech/) (libSQL) with native vector type** (edge sync, hosted)

   If you need edge replication, serverless endpoints, and built-in vector search in a SQLite‑compatible service, Turso/libSQL's **native vector datatype** is a strong option. This is not strictly local/offline, but is a low‑ops path when you outgrow single‑file SQLite and want transparent sync + vector search. Our schemas remain compatible.

3. **[Meilisearch](https://www.meilisearch.com/)** (hybrid lexical+vector search engine)

   If the product needs full‑text ranking, typo tolerance, facets, and semantic re‑ranking, stand up **Meilisearch** alongside SQLite. It supports vector/hybrid search with user-provided or provider-generated embeddings. Treat it as a **secondary index** (read‑optimized) fed from SQLite via an indexing job. Not a replacement for the primary DB.

## Rationale

- **Portability & simplicity**: SQLite is a single file; `sqlite-vec` is a single C extension, no external dependencies. Works in WASM and on constrained devices.
- **Performance**: HNSW‑style ANN search is sufficiently fast for local experiments and moderate corpora (10^5–10^6 vectors with tuned parameters). We can shard by collection or project when needed.
- **Developer UX**: One process, no daemon to install; easy to vendor the extension and migrate with Alembic/SQLModel.
- **Future‑proofing**: The schema maps cleanly to Postgres+pgvector or to Turso/libSQL's native vector type if we later need multi‑writer, sync, or hosted scale.

## Implementation details

- **ORM**: Continue defining entities with SQLModel/SQLAlchemy. Store embeddings in a dedicated `vec0` virtual table (from `sqlite-vec`) keyed by our primary entity IDs; keep metadata in normal relational tables.
- **Distances**: Standardize on cosine for text embeddings, with optional L2/IP when required.
- **Indexes**: Create HNSW indexes via the extension where supported; tune M, efConstruction, and efSearch per collection size.
- **Migrations**: Package `sqlite-vec` with the app (or load at runtime). Provide `pragma`/`SELECT` health checks to verify the extension is loaded. Migration scripts create virtual tables and seed initial indexes.
- **Testing**: Fixture to bootstrap a fresh SQLite DB and populate vectors; golden tests for recall@k on known query sets.

### Sketch schema (illustrative)

```sql
-- Documents and chunks (relational)
CREATE TABLE document (
  id TEXT PRIMARY KEY,
  title TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE chunk (
  id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL REFERENCES document(id) ON DELETE CASCADE,
  content TEXT NOT NULL,
  token_count INTEGER NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Vector table via sqlite-vec
CREATE VIRTUAL TABLE chunk_embedding USING vec0(
  id TEXT PRIMARY KEY,           -- same as chunk.id
  embedding float[1536],         -- adjust to model dims
  partition_key TEXT             -- optional for sharding/collections
);

-- HNSW index (params depend on corpus size)
CREATE INDEX chunk_embedding_hnsw ON chunk_embedding(embedding)
  WITH (method = hnsw, M = 32, efConstruction = 200);
```

### Query examples

```sql
-- Top‑k semantic neighbors for a query vector
SELECT c.id, c.document_id, c.content,
       array_cosine_distance(e.embedding, :q) AS distance
FROM chunk_embedding e
JOIN chunk c ON c.id = e.id
ORDER BY distance
LIMIT 10;

-- Hybrid: BM25 prefilter + vector rerank (when paired with FTS5)
WITH candidates AS (
  SELECT c.id FROM chunk c
  JOIN chunk_fts ON chunk_fts.rowid = c.rowid
  WHERE chunk_fts MATCH :lexical
  ORDER BY bm25(chunk_fts)
  LIMIT 200
)
SELECT c.id, c.content,
       array_cosine_distance(e.embedding, :q) AS distance
FROM candidates x
JOIN chunk_embedding e ON e.id = x.id
JOIN chunk c ON c.id = x.id
ORDER BY distance
LIMIT 10;
```

## Trade‑offs & risks

- **Write‑heavy vector updates**: HNSW maintenance cost can be noticeable for frequent upserts; batch builds or periodic reindexing may be required for very large corpora.
- **Multi‑writer**: SQLite is single‑writer; acceptable for local dev and CI, but not for many concurrent writers. For that, graduate to Postgres+pgvector or Turso.
- **Model churn**: Changing embedding dimensions requires rebuilding indexes; we mitigate via per‑model collections.

## Migration path (if/when needed)

- **Postgres + pgvector**: Map embedding table to `vector` type, recreate HNSW/IVFFlat indexes, keep relational schema nearly identical.

## Related ADRs

<!-- Reference numbers of related decisions
Links to dependent or impacted architectural decisions -->

- [005 - Chunking](./005-chunking.md)
- [007 - Indexing, Search, Retrieval](./007-index-search-retrieval.md)

## Additional Notes

<!-- Any supplementary information
References to documentation
Contact person for further questions -->
