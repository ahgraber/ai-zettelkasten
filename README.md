# AI-Zettelkasten

An AI-driven [Zettelkasten](https://zettelkasten.de/introduction/)-style mindmap and assistant for "talk to my data" and deep research over web-based resources.

This project is intended to be self-hosted with minimal infrastructure requirements - a mini PC with a multicore processor and 8+ GB RAM should suffice. Infrastructure components should be manageable by a 'compose' stack (_note: though I'll be hosting on a k3s cluster_). This means no GPU requirements; AI inference is provided through API services.

## What is a Zettelkasten?

A Zettelkasten is a way of connecting atomic ideas into a linked (hypertextual), personal knowledge graph.

1. Each Node ("zettel", from German "slip" or "note") is atomic, containing a single concept, idea, or fact
2. Nodes are interconnected with links. _A Zettelkasten makes **connecting** and not **collecting** a priority._
3. A Zettelkasten is unique, resulting from knowledge processing over an individual corpus.

Each node must have:

1. A unique address - Defined by a hash based on the content of the note. `xxHash` might be used for an exact hash, `minhash` for textual similarity (i.e., similar words, letters), and/or `semhash` for semantic similarity.
2. Content - The individual (atomic) piece of knowledge.
3. References - The source reference(s) for the content.

In a traditional Zettelkasten, the zettel body would contain links to other nodes. In the AI Zettelkasten, these are defined as an additional Relationship that contains source/destination directionality, relationship type, and other metadata.

Zettelkasten may also benefit from structural notes that create hierarchy, serving as aggregator or summary nodes about a broader (but still atomic!) concept that incorporates or relates to multiple, more granular nodes.

- [Introduction to the Zettelkasten Method • Zettelkasten Method](https://zettelkasten.de/introduction/)
- [Forget Forgetting. Build a Zettelkasten.](https://every.to/superorganizers/forget-forgetting-build-a-zettelkasten-299960)

## Design

### Data Flow

1. Collect: use [Karakeep](https://karakeep.app/) as a content management system for bookmarking and archiving web-based resources.
   - Karakeep archives content and extracts text content when possible, but specialized content extraction & parsing will perform better for archived files (such as PDFs from arxiv.org).
2. Parse: Extract, and clean content with [docling-project/docling](https://github.com/docling-project/docling/tree/main). Export markdown and extracted images to S3-compatible blob storage.
3. Chunk
4. Embed
5. Index
6. Retrieve (search, rerank)
7. Respond
8. Research
9. Explore

## Devshell

This repo uses nix devshells to manage project dependencies.

Use node2nix to create `node-env.nix` from `package.json` `node-env.nix` will be picked up in the flake devshell

```sh
node2nix -i package.json -o ./nix/node-packages.nix -c ./nix/default.nix -e ./nix/node-env.nix -18
```
