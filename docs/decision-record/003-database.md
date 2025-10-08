# 003 - Database(s)

## Status

21 June 2025 - Proposed 8 October 2025 – Revised

<!-- Proposed/Accepted/Revised/Deprecated/Superseded -->

## Context

The AI Zettelkasten manages a large volume of structured data over multiple transformations (documents, chunks, relationships, metadata, embeddings, etc.).
These transformations are computationally expensive, so intermediate and final results must be persisted and efficiently retrievable by both primary-key lookups and vector similarity queries.

Constraints and preferences:

- **Local-first**: should run fully offline on developer laptops and CI with minimal services.
- **Embed-friendly**: embeddable engine preferred over client/server.
- **Simple integration**: leverage SQLModel/SQLAlchemy where possible.
- **Vector-native**: support approximate nearest neighbor (ANN) indexes (e.g., HNSW) and common distances (cosine/L2/IP).
- **Future migration**: ability to move to a networked store later without major model changes.

## Decision

### Selected approach: **SQLite + `sqlite-vec`** (local-first)

Use **SQLite** as the primary local, embedded OLTP store and add **`sqlite-vec`** as a runtime extension to persist and query embedding vectors.
`sqlite-vec` is a no-dependency C extension that works anywhere SQLite runs (Linux/macOS/Windows, WASM, mobile) and exposes a simple SQL interface via virtual tables.
This keeps the system maximally portable while providing fast, HNSW-backed ANN search that is "good enough" for local development and small/medium datasets.

Note: see [slaily/aiosqlitepool: 🛡️A resilient, high-performance asynchronous connection pool layer for SQLite, designed for efficient and scalable database operations.](https://github.com/slaily/aiosqlitepool)

SQLite UIs:

- [SQLiteViewer](https://alpha.sqliteviewer.app)
- [Beekeeper Studio](https://www.beekeeperstudio.io/)

> Why SQLite over DuckDB for the default? SQLite is a general-purpose, transactional store with battle-tested concurrency semantics for application state. DuckDB excels at OLAP and columnar analytics; we still use DuckDB for ad‑hoc analysis, but SQLite fits day‑to‑day app storage, migrations, and packaging better. With `sqlite-vec`, SQLite now covers vector needs as well.

### Alternatives and when to choose them

1. **[DuckDB](https://duckdb.org/) + [VSS extension](https://duckdb.org/docs/stable/core_extensions/vss.html)** (analytics‑heavy or columnar workloads)

   Choose when you primarily run analytical queries, large scans, or want columnar performance.
   DuckDB's **VSS** extension provides HNSW indexes over fixed-size list columns and integrates cleanly with array functions, joins, and lateral queries.
   Keep DuckDB for notebooks and batch pipelines; optionally mirror vectors from SQLite for unified retrieval.

   [Duck UI](https://demo.duckui.com/) is an open-source UI for DuckDB.

2. **[Turso](https://turso.tech/) (libSQL) with native vector type** (edge sync, hosted)

   If you need edge replication, serverless endpoints, and built-in vector search in a SQLite‑compatible service, Turso/libSQL's **native vector datatype** is a strong option.
   This is not strictly local/offline, but is a low‑ops path when you outgrow single‑file SQLite and want transparent sync + vector search.
   Our schemas remain compatible.

3. **[Meilisearch](https://www.meilisearch.com/)** (hybrid lexical+vector search engine)

   If the product needs full‑text ranking, typo tolerance, facets, and semantic re‑ranking, stand up **Meilisearch** alongside SQLite.
   It supports vector/hybrid search with user-provided or provider-generated embeddings.
   Treat it as a **secondary index** (read‑optimized) fed from SQLite via an indexing job.
   Not a replacement for the primary DB.

## Rationale

- **Portability & simplicity**: SQLite is a single file; `sqlite-vec` is a single C extension, no external dependencies.
  Works in WASM and on constrained devices.
- **Performance**: HNSW‑style ANN search is sufficiently fast for local experiments and moderate corpora (10^5–10^6 vectors with tuned parameters).
  We can shard by collection or project when needed.
- **Developer UX**: One process, no daemon to install; easy to vendor the extension and migrate with Alembic/SQLModel.
- **Future‑proofing**: The schema maps cleanly to Postgres+pgvector or to Turso/libSQL's native vector type if we later need multi‑writer, sync, or hosted scale.

## Implementation details

- **ORM**: Continue defining entities with SQLModel/SQLAlchemy.
  Store embeddings in a dedicated `vec0` virtual table (from `sqlite-vec`) keyed by our primary entity IDs; keep metadata in normal relational tables.
- **Distances**: Standardize on cosine for text embeddings, with optional L2/IP when required.
- **Indexes**: Create HNSW indexes via the extension where supported; tune M, efConstruction, and efSearch per collection size.
- **Migrations**: Package `sqlite-vec` with the app (or load at runtime).
  Provide `pragma`/`SELECT` health checks to verify the extension is loaded.
  Migration scripts create virtual tables and seed initial indexes.
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
- **Multi‑writer**: SQLite is single‑writer; acceptable for local dev and CI, but not for many concurrent writers.
  For that, graduate to Postgres+pgvector or Turso.
- **Multi‑node deployments**: SQLite is a local file.
  If API and workers run on different Kubernetes nodes, they must share the same RWX volume mounted at the same path; reliability depends on the storage backend.
  If a shared volume is not available or stable, use Postgres instead.
- **Model churn**: Changing embedding dimensions requires rebuilding indexes; we mitigate via per‑model collections.

## Migration path (if/when needed)

- **Postgres + pgvector**: Map embedding table to `vector` type, recreate HNSW/IVFFlat indexes, keep relational schema nearly identical.

## Addendum: Litestream Continuous Replication

30 March 2026

### Context

SQLite is a local file.
If the host dies, the database is gone.
The conversion service stores job state and output metadata that is expensive to reconstruct (re-running all conversions), so durability beyond the local disk matters even for a single-node deployment.

Additionally, deploying to different nodes (or rebuilding a container) currently requires either mounting a shared volume for the SQLite file or accepting data loss.
A replication strategy that uses S3 as the source of truth lets new nodes restore the database to local storage on startup, avoiding the need for cloud-mounted volumes or persistent disk claims.

### Decision: Litestream for continuous S3 replication

Use [Litestream](https://litestream.io/) to continuously replicate the SQLite WAL to S3-compatible object storage.
Litestream monitors the WAL file, copies new frames to a shadow WAL, and uploads them to S3 on a configurable interval.
On startup, a restore step downloads the latest replica and reconstructs the database before the application opens it.

This provides:

- **Point-in-time recovery** without scheduled dump jobs or cron
- **Minimal operational overhead** — Litestream runs as a sidecar subprocess managed by the application (`LitestreamManager` in `src/aizk/conversion/utilities/litestream.py`)
- **S3 as the durability layer** — the same bucket already used for conversion artifacts

### Alternatives considered

| Alternative                             | Why not                                                                                                                                                           |
| --------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Scheduled `sqlite3 .backup`**         | Coarse RPO (minutes to hours between backups); requires cron or equivalent; backup during heavy writes can stall the writer                                       |
| **LiteFS (FUSE-based replication)**     | Designed for multi-node read replicas with primary forwarding; adds FUSE dependency and operational complexity beyond what a single-node deployment needs         |
| **Postgres from the start**             | Adds a network dependency and operational surface (provisioning, connection pooling, backups) that isn't justified at current scale; see migration triggers below |
| **No replication (local backups only)** | Acceptable for development; not acceptable for any deployment where re-running all conversions is costly                                                          |

### Trade-offs accepted

- **Single-writer assumption.**
  Litestream assumes one writer process.
  The conversion service has two (API + worker).
  This works under low write throughput because SQLite serializes writers at the WAL level, but creates risks under sustained load (see write topology addendum below).
- **No multi-node replicas.**
  Litestream replicates to S3, not to another SQLite instance.
  Read scaling requires a different tool (LiteFS) or migrating to Postgres.
- **Restore is not instant.**
  Startup downloads the full database from S3 before the application is ready.
  For large databases this adds startup latency.
- **Litestream is a young project.**
  Known bugs exist under high write load (see topology addendum).
  The project is maintained but not commercially supported.

### Configuration

Litestream is controlled by environment variables surfaced through `ConversionConfig`:

| Field                            | Default | Purpose                                                    |
| -------------------------------- | ------- | ---------------------------------------------------------- |
| `litestream_enabled`             | `True`  | Global on/off                                              |
| `litestream_start_role`          | `"api"` | Which process runs Litestream (`api`, `worker`, or `both`) |
| `litestream_restore_on_startup`  | `True`  | Restore from S3 before opening the database                |
| `litestream_allow_empty_restore` | `True`  | Tolerate missing S3 replica on first run                   |
| `litestream_s3_bucket_name`      | `""`    | S3 bucket (falls back to the shared `s3_bucket_name`)      |
| `litestream_s3_prefix`           | `"db"`  | S3 key prefix for replica files                            |

---

## Addendum: SQLite Write Topology and Litestream Replication

30 March 2026

### Write topology

The conversion service runs two processes that write to the same SQLite database:

| Process                          | Write operations                                   | Transaction style                                      |
| -------------------------------- | -------------------------------------------------- | ------------------------------------------------------ |
| **API** (`conversion-api`)       | Job submission, retry, cancel, bulk actions        | `BEGIN IMMEDIATE` for all write endpoints              |
| **Worker** (`conversion-worker`) | Job claiming, status transitions, output recording | `BEGIN IMMEDIATE` for job claiming; implicit elsewhere |

Both processes mount the same data volume and share a single SQLite file.

### SQLite WAL concurrency model

SQLite in WAL mode allows concurrent readers alongside a single writer.
The write lock (RESERVED) serializes writers but does not block readers.

All connections are configured with these PRAGMAs (see `src/aizk/conversion/db.py`):

| PRAGMA               | Value    | Purpose                                                                          |
| -------------------- | -------- | -------------------------------------------------------------------------------- |
| `journal_mode`       | `WAL`    | Concurrent readers + writer; required by Litestream                              |
| `synchronous`        | `NORMAL` | Sync at checkpoint only; WAL provides crash recovery                             |
| `busy_timeout`       | `5000`   | Wait up to 5 s for the write lock before returning `SQLITE_BUSY`                 |
| `foreign_keys`       | `ON`     | SQLite disables referential integrity by default                                 |
| `wal_autocheckpoint` | `0`      | Disable SQLite's autocheckpoint so Litestream controls checkpointing exclusively |

### Transaction locking: BEGIN IMMEDIATE

Write endpoints in both the API and worker use `BEGIN IMMEDIATE` to acquire the write lock at transaction start.

**Why not deferred transactions (the default)?**
In WAL mode, a deferred read transaction that later attempts a write upgrade can fail with `SQLITE_BUSY_SNAPSHOT` if another writer has committed since the read snapshot began.
This failure is **immediate and non-retriable** — `busy_timeout` does not help because the problem is a stale snapshot, not a held lock.

`BEGIN IMMEDIATE` moves the contention point to `BEGIN` time where `busy_timeout` works correctly: SQLite sleeps and retries for up to the configured timeout, waiting for the other writer to finish.

**Common misconception:** `BEGIN IMMEDIATE` acquires a RESERVED lock, not an EXCLUSIVE lock.
In WAL mode, RESERVED serializes writers (which SQLite does regardless) while allowing all readers to proceed freely.
The "exclusive write lock on the entire database" characterization is misleading — readers are never blocked.

### Litestream replication constraints

Litestream replicates SQLite WAL frames to S3 for continuous backup.
It takes over the checkpointing process by maintaining a long-running read transaction.

**Operational rules:**

1. **Single Litestream instance only.**
   The `litestream_start_role` config controls which process runs Litestream (default: `"api"`).
   Never set `litestream_start_role` to `"both"` — two Litestream instances replicating to the same S3 path will corrupt the replica.

2. **Autocheckpoint must be disabled.**
   SQLite's default autocheckpoint (every 1000 WAL pages) races with Litestream's own checkpoint schedule.
   When Litestream detects a WAL discontinuity from an external checkpoint, it triggers an expensive full-snapshot re-upload.
   `PRAGMA wal_autocheckpoint=0` prevents this.

3. **Monitor Litestream generations.**
   A new generation in Litestream's S3 output indicates it detected a WAL discontinuity — likely a checkpoint race or unclean shutdown.
   Frequent new generations under normal operation signal a configuration problem.

**Known Litestream issues under high write load:**

- [benbjohnson/litestream#1198](https://github.com/benbjohnson/litestream/issues/1198): Concurrent writes between sync operations cause false WAL discontinuity detection, triggering full snapshots (500+ writes/sec).
  Open bug.
- [benbjohnson/litestream#1037](https://github.com/benbjohnson/litestream/issues/1037), [#1083](https://github.com/benbjohnson/litestream/issues/1083): Silent replication failures when WAL size stays unchanged (SQLite reuses WAL space).

**SQLite version:** Verify the runtime SQLite version is ≥ 3.51.3 to avoid the WAL-reset corruption bug (data race during concurrent write + checkpoint that can skip pages during checkpointing; present in all versions 3.7.0–3.51.2).

### When to migrate to Postgres

The SQLite + Litestream topology is appropriate for single-node, low-to-moderate write throughput deployments.
Migrate to Postgres + pgvector when any of the following apply:

- **Multi-node deployment** required and a shared RWX volume is unavailable or unreliable.
- **Sustained write throughput** exceeds ~100 writes/sec, where Litestream checkpoint races become likely.
- **Multiple worker instances** are needed on separate hosts (SQLite's file-level locking requires co-located processes).
- **Litestream bugs** in the known-issues list are triggered by production workload patterns.

The migration path is straightforward: SQLModel/SQLAlchemy schemas map to Postgres with minimal changes, and `sqlite-vec` indexes map to pgvector HNSW indexes.

## Related ADRs

- [005 - Chunking](./005-chunking.md)
- [007 - Indexing, Search, Retrieval](./007-index-search-retrieval.md)
