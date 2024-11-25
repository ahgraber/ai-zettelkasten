# RAG Assistant

A RAG-based research assistant

## Design

### Ingest

- User lists URLs to scrape
- Links added to DB and queued
- Scrape downloads page or pdf (ArchiveBox?) to disk (S3?) and updates status, filepath
- Text extractor (Docling?) runs on downloaded content
- Chunking, Embedding & indexing (LlamaIndex?) to docstore and vector index (postgres w/ pgvector?)

### Inference

- [Cinnamon/kotaemon: An open-source RAG-based tool for chatting with your documents.](https://github.com/Cinnamon/kotaemon)
- [SciPhi-AI/R2R: Containerized, state of the art Retrieval-Augmented Generation (RAG) system with a RESTful API](https://github.com/SciPhi-AI/R2R/tree/main)
