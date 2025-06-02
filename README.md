# AI-Zettelkasten

An AI-driven [Zettelkasten](https://zettelkasten.de/introduction/)-style mindmap and assistant for deep research.

## Design

### What is a Zettelkasten?

[Introduction to the Zettelkasten Method • Zettelkasten Method](https://zettelkasten.de/introduction/)
[Forget Forgetting. Build a Zettelkasten.](https://every.to/superorganizers/forget-forgetting-build-a-zettelkasten-299960)

### Ingest

- User lists URLs to scrape
- Links added to DB and queued
- Scrape downloads page or pdf (ArchiveBox?) to disk (S3?) and updates status, filepath
- Text extractor (Docling?) runs on downloaded content
- Chunking, Embedding & indexing (LlamaIndex?) to docstore and vector index (postgres w/ pgvector?)

#### Extractors

```sh
npm install @postlight/parser
npm install 'git+https://github.com/pirate/readability-extractor'
npm install 'single-file-cli@1.1.54' # version is important!
```

### Inference

- [Cinnamon/kotaemon: An open-source RAG-based tool for chatting with your documents.](https://github.com/Cinnamon/kotaemon)
- [SciPhi-AI/R2R: Containerized, state of the art Retrieval-Augmented Generation (RAG) system with a RESTful API](https://github.com/SciPhi-AI/R2R/tree/main)

## Devshell

This repo uses nix devshells to manage project dependencies.

Use node2nix to create `node-env.nix` from `package.json` `node-env.nix` will be picked up in the flake devshell

```sh
node2nix -i package.json -o ./nix/node-packages.nix -c ./nix/default.nix -e ./nix/node-env.nix -18
```
