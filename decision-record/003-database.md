# 003 - Database(s)

## Status

21 June 2025 - Proposed

<!-- Proposed/Accepted/Revised/Deprecated/Superseded -->

## Context

The AI Zettelkasten requires managing a large volume of data over multiple transformations (documents, chunks, relationships, metadata, etc.); these transformations are computationally expensive, so intermediate and final results must be persisted. While file-based storage (e.g., JSON, pickle) is simple, it becomes limiting as the system grows.

Database choice should be driven by simple integration, ideally using a shared data model (via SQLModel/SQLAlchemy), for smooth migration between prototype and production use cases.

- [DuckDB – An in-process SQL OLAP database management system](https://duckdb.org/)
- [SQLite Home Page](https://sqlite.org/)
- [PostgreSQL: The world's most advanced open source database](https://www.postgresql.org/)
- [microsoft/documentdb: DocumentDB offers a native implementation of document-oriented NoSQL database, enabling seamless CRUD operations on BSON data types within a PostgreSQL framework.](https://github.com/microsoft/documentdb)
- [MongoDB Community Edition | MongoDB](https://www.mongodb.com/try/download/community)
- [Deployment options - Neo4j Documentation](https://neo4j.com/docs/deployment-options/)
- [Memgraph download hub](https://memgraph.com/download)

## Decision

### Selected Approach: **[DuckDB](https://duckdb.org/)**

### Rationale

[DuckDB](https://duckdb.org/) offers a local, embedded database using a (mostly) postgresql-compliant dialect. DuckDB also has core extensions that support AI-native workflows, including:

- [Vector Similarity Search Extension](https://duckdb.org/docs/stable/core_extensions/vss.html)
- [Full-Text Search Extension](https://duckdb.org/docs/stable/core_extensions/full_text_search.html)
- [httpfs Extension](https://duckdb.org/docs/stable/core_extensions/httpfs/overview) provides [S3 API Support](https://duckdb.org/docs/stable/core_extensions/httpfs/s3api)

This supports an "easy" migration to Postgres given the capability overlap, and assuming the data layer is abstracted with SQLModel and/or SQLAlchemy.

### Alternative Considered

#### Option 1: SQLite

SQLite is a good choice for an embedded database for simple CRUD operations. However, it has limited extension support for AI-native use cases, especially vector operations. While [asg017/sqlite-vec](https://github.com/asg017/sqlite-vec) does provide this functionality, the current DuckDB extension provides HNSW support.

#### Option 2: NoSQL (MongoDB, DocumentDB)

The data requirements for AI Zettelkasten fall more into structured, predefined schema as opposed to requiring evolving metadata fields or shapes. In the even that metadata flexibility is required, a "mullet schema" can be used in a relational database using a JSON-type column to flexibly handle evolving metadata.

## Implementation Details

Define tables using SQLModel; this allows changing the database engine without requiring massive codebase updates for the migration.

## Related ADRs

<!-- Reference numbers of related decisions
Links to dependent or impacted architectural decisions -->

- [005 - Chunking](./005-chunking.md)
- [007 - Indexing, Search, Retrieval](./007-index-search-retrieval.md)

## Additional Notes

<!-- Any supplementary information
References to documentation
Contact person for further questions -->
