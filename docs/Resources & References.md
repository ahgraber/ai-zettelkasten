# Resources & References

- [Introduction to the Zettelkasten Method - Zettelkasten Method](https://zettelkasten.de/introduction/)
- [From Vector Search to Entity Processing: Evolving Zettelgarden's Connection Engine - Zettelgarden](https://zettelgarden.com/blog/entities)
  and [HN Discussion](https://news.ycombinator.com/item?id=42577387)

## LLM knowledge base

Alternative to RAG: use LLMs to maintain an evolving markdown library as a personal knowledge base.

- [Andrej Karpathy on X: "LLM Knowledge Bases..."](https://x.com/karpathy/status/2039805659525644595) — original thread
- [llm-wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)
- [Karpathy shares 'LLM Knowledge Base' architecture that bypasses RAG with an evolving markdown library maintained by AI | VentureBeat](https://venturebeat.com/data/karpathy-shares-llm-knowledge-base-architecture-that-bypasses-rag-with-an)
- [Lex Fridman on X: similar setup — Obsidian + Cursor + vibe-coded web terminals](https://x.com/lexfridman/status/2039841897066414291)
- [Karpathy Just Described the Product I've Been Building — LLM Knowledge Bases and YARNNN | yarnnn](https://www.yarnnn.com/blog/karpathy-just-described-the-product-ive-been-building)
- [Your File System Is Already a Graph Database](https://rumproarious.com/2026/04/04/your-file-system-is-already-a-graph-database/)

## Ingest / Archiving

- [ArchiveBox | 🗃 Open source self-hosted web archiving. Takes URLs/browser history/bookmarks/Pocket/Pinboard/etc., saves HTML, JS, PDFs, media, and more…](https://archivebox.io/)
- [dgtlmoon/changedetection.io: The best and simplest free open source web page change detection, website watcher, restock monitor and notification service.](https://github.com/dgtlmoon/changedetection.io/tree/master)
  - [dgtlmoon/pyppeteer-ng: Headless chrome/chromium automation library (unofficial port of puppeteer)](https://github.com/dgtlmoon/pyppeteer-ng/tree/dev?tab=readme-ov-file)
- [karakeep-app/karakeep: A self-hostable bookmark-everything app (links, notes and images) with AI-based automatic tagging and full text search](https://github.com/karakeep-app/karakeep) — uses monolith for archiving (formerly hoarder)
- [gildas-lormeau/SingleFile: Web Extension for saving a faithful copy of a complete web page in a single HTML file](https://github.com/gildas-lormeau/SingleFile)
- [Y2Z/monolith: ⬛️ CLI tool for saving complete web pages as a single HTML file](https://github.com/Y2Z/monolith)
- [postlight/parser: 📜 Extract meaningful content from the chaos of a web page](https://github.com/postlight/parser)
- [cyclotruc/gitingest: Replace 'hub' with 'ingest' in any github url to get a prompt-friendly extract of a codebase](https://github.com/cyclotruc/gitingest/tree/main)
- [databridge-org/databridge-core: Multi-modal modular data ingestion and retrieval](https://github.com/databridge-org/databridge-core)
- [List of Chromium Command Line Switches](https://peter.sh/experiments/chromium-command-line-switches/)

- [chonkie-inc/chonkie - RAG chunking library](https://github.com/chonkie-inc/chonkie)
- [Dense X Retrieval: What Retrieval Granularity Should We Use?](https://arxiv.org/abs/2312.06648)
- [Claimify: Extracting Claims from LM Outputs (Microsoft Research)](https://www.microsoft.com/en-us/research/blog/claimify-extracting-high-quality-claims-from-language-model-outputs/)
- [Claim Extraction from LLM Outputs](https://arxiv.org/abs/2502.10855)
- [Contextual Retrieval (Anthropic)](https://www.anthropic.com/news/contextual-retrieval)
- [Late Chunking in Long-Context Embedding Models (Jina)](https://jina.ai/news/late-chunking-in-long-context-embedding-models/)
- [Decoupling Retrieval vs. Synthesis Chunks (LlamaIndex)](https://docs.llamaindex.ai/en/stable/optimizing/production_rag/#decoupling-chunks-used-for-retrieval-vs-chunks-used-for-synthesis)

- [Optimize parsing costs with LlamaParse auto mode — LlamaIndex](https://www.llamaindex.ai/blog/optimize-parsing-costs-with-llamaparse-auto-mode)
- [DS4SD/docling: Get your documents ready for gen AI](https://github.com/DS4SD/docling) and [DS4SD/docling-serve: Running Docling as an API service](https://github.com/DS4SD/docling-serve)
- [getomni-ai/zerox: PDF to Markdown with vision models](https://github.com/getomni-ai/zerox)
- [VikParuchuri/marker: Convert PDF to markdown + JSON quickly with high accuracy](https://github.com/VikParuchuri/marker)
- [mindee/doctr: docTR (Document Text Recognition) - a seamless, high-performing & accessible library for OCR-related tasks powered by Deep Learning.](https://github.com/mindee/doctr)
- [Zotero | Your personal research assistant](https://www.zotero.org/)
  - [urschrei/pyzotero: Pyzotero: a Python client for the Zotero API](https://github.com/urschrei/pyzotero)
  - [DIY: Ground LLaMa on your papers from Zotero | by Emmett McFarlane | Medium](https://medium.com/@emcf1/diy-ground-a-language-model-on-your-papers-from-zotero-with-finesse-a5c4ca7c187a)
- [emcf/thepipe: Extract clean data from anywhere, powered by vision-language models ⚡](https://github.com/emcf/thepipe)
- [microsoft/markitdown: Python tool for converting files and office documents to Markdown.](https://github.com/microsoft/markitdown)
- [cyclotruc/gitdigest: Web interface to turn codebases into prompt-friendly text](https://github.com/cyclotruc/gitdigest?tab=readme-ov-file)
- [[2408.15836] Knowledge Navigator: LLM-guided Browsing Framework for Exploratory Search in Scientific Literature](https://arxiv.org/abs/2408.15836)
- [bytedance/Dolphin: Document Image Parsing via Heterogeneous Anchor Prompting](https://github.com/bytedance/Dolphin)
- [opendatalab/MinerU: A high-quality tool to convert PDF to Markdown and JSON](https://github.com/opendatalab/MinerU/tree/master)
- [nanonets/Nanonets-OCR-s · Hugging Face](https://huggingface.co/nanonets/Nanonets-OCR-s)
- [Unstructured - Open Source](https://docs.unstructured.io/open-source/introduction/overview)
- [LLMWhisperer: Make Complex Document Data Ready for LLMs](https://unstract.com/llmwhisperer/)
- [morphik-org/morphik-core: Open source multi-modal RAG for building AI apps over private knowledge.](https://github.com/morphik-org/morphik-core)
- [Supercharge your OCR Pipelines with Open Models](https://huggingface.co/blog/ocr-open-models)

### parse benchmarks

- [[2410.09871] A Comparative Study of PDF Parsing Tools Across Diverse Document Categories](https://arxiv.org/abs/2410.09871)
- [[2412.02592] OCR Hinders RAG: Evaluating the Cascading Impact of OCR on Retrieval-Augmented Generation](https://arxiv.org/abs/2412.02592)
- [[2412.07626] OmniDocBench: Benchmarking Diverse PDF Document Parsing with Comprehensive Annotations](https://arxiv.org/abs/2412.07626)
- [[2501.00321] OCRBench v2: An Improved Benchmark for Evaluating Large Multimodal Models on Visual Text Localization and Reasoning](https://arxiv.org/abs/2501.00321)
- [Intelligent Document Processing Leaderboard](https://idp-leaderboard.org/)
- [getomni-ai/benchmark: OCR Benchmark](https://github.com/getomni-ai/benchmark)
- [allenai/olmocr bench](https://github.com/allenai/olmocr/tree/main/olmocr/bench)

- [OpenAI text-embedding-3-small](https://platform.openai.com/docs/models/text-embedding-3-small)
- [OpenAI text-embedding-3-large](https://platform.openai.com/docs/models/text-embedding-3-large)
- [Cohere Embed](https://cohere.com/embed)
- [Cohere Rerank](https://cohere.com/rerank)
- [Voyage AI Embeddings](https://docs.voyageai.com/docs/embeddings)
- [Jina Embeddings](https://jina.ai/embeddings/)

- [chonkie-ai/chonkie: 🦛 CHONK your texts with Chonkie ✨ - The no-nonsense RAG chunking library](https://github.com/chonkie-ai/chonkie)
- [[2312.06648] Dense X Retrieval: What Retrieval Granularity Should We Use?](https://arxiv.org/abs/2312.06648) — deconstruct text blobs into propositions (complete factoids)
- [Contextual Retrieval — Anthropic](https://www.anthropic.com/news/contextual-retrieval)
- [Late Chunking in Long-Context Embedding Models](https://jina.ai/news/late-chunking-in-long-context-embedding-models/)
- [Decoupled chunk representations — LlamaIndex](https://docs.llamaindex.ai/en/stable/optimizing/production_rag/#decoupling-chunks-used-for-retrieval-vs-chunks-used-for-synthesis) — separate retrieval chunks from synthesis chunks

- [PyLate - Late Interaction Models](https://lightonai.github.io/pylate/)
  - [lightonai/pylate (GitHub)](https://github.com/lightonai/pylate)
  - [lightonai/fast-plaid](https://github.com/lightonai/fast-plaid)
- [LlamaIndex](https://docs.llamaindex.ai/en/stable/)
- [Late Interaction: Efficient Multi-Modal Retrievers (LanceDB)](https://lancedb.com/blog/late-interaction-efficient-multi-modal-retrievers-need-more-than-just-a-vector-index/)
- [Late Interaction Overview (Weaviate)](https://weaviate.io/blog/late-interaction-overview)

### Sparse Retrieval

- [naver/splade - sparse neural search](https://github.com/naver/splade)
- [From grep to SPLADE: a journey through semantic search (Elicit)](https://blog.elicit.com/semantic-search/)
- [SPLADE for Sparse Vector Search Explained (Pinecone)](https://www.pinecone.io/learn/splade/)
- [TILDE: Fast Passage Re-ranking with Contextualized Exact Term Matching](https://arxiv.org/abs/2108.08513)
- [Context-Aware Document Term Weighting for Ad-Hoc Search](https://dl.acm.org/doi/pdf/10.1145/3366423.3380258)

If building a concept graph, it would be useful to be able to use it as a human in addition to have it be searchable for RAG.

- [SQLite Viewer](https://alpha.sqliteviewer.app)
- [Beekeeper Studio](https://www.beekeeperstudio.io/)
- [DuckDB](https://duckdb.org/)
  - [DuckDB VSS extension](https://duckdb.org/docs/stable/core_extensions/vss.html)
- [Turso - edge SQLite](https://turso.tech/)
- [Meilisearch](https://www.meilisearch.com/)
- [Litestream - SQLite replication](https://litestream.io/)
- [slaily/aiosqlitepool - async SQLite connection pool](https://github.com/slaily/aiosqlitepool)

## Model Providers / Frameworks

- bm25
- Learned Sparse Retrieval
  - SPLADE
    - [naver/splade: SPLADE: sparse neural search (SIGIR21, SIGIR22)](https://github.com/naver/splade?tab=readme-ov-file)
    - [From grep to SPLADE: a journey through semantic search](https://blog.elicit.com/semantic-search/)
    - [SPLADE for Sparse Vector Search Explained | Pinecone](https://www.pinecone.io/learn/splade/)
  - TILDE - [[2108.08513] Fast Passage Re-ranking with Contextualized Exact Term Matching and Efficient Passage Expansion](https://arxiv.org/abs/2108.08513)
  - [Context-Aware Document Term Weighting for Ad-Hoc Search](https://dl.acm.org/doi/pdf/10.1145/3366423.3380258)
- vector

## Orchestration

- [Cinnamon/kotaemon: An open-source RAG-based tool for chatting with your documents.](https://github.com/Cinnamon/kotaemon)
- [Chainlit/chainlit: Build Conversational AI in minutes ⚡️](https://github.com/Chainlit/chainlit)
- [CopilotKit/CopilotKit: React UI + elegant infrastructure for AI Copilots, in-app AI agents, AI chatbots, and AI-powered Textareas 🪁](https://github.com/CopilotKit/CopilotKit)
- [AnswerDotAI/fasthtml: The fastest way to create an HTML app](https://github.com/AnswerDotAI/fasthtml)
  - [MonsterUI: Bringing Beautiful UI to FastHTML – Answer.AI](https://www.answer.ai/posts/2025-01-15-monsterui.html)
- [reflex-dev/reflex: 🕸️ Web apps in pure Python 🐍](https://github.com/reflex-dev/reflex)
- [tobi/qmd: mini cli search engine for your docs, knowledge bases, meeting notes, whatever. Tracking current sota approaches while being all local](https://github.com/tobi/qmd)
- [Building AI Products—Part I: Back-end Architecture](https://philcalcado.com/2024/12/14/building-ai-products-part-i.html)
- [Introduction — Build, scale, and manage user-facing Retrieval-Augmented Generation applications.](https://r2r-docs.sciphi.ai/introduction)
- [Future-House/paper-qa: High accuracy RAG for answering questions from scientific documents with citations](https://github.com/Future-House/paper-qa)
- [prsdm/smart-rag](https://github.com/prsdm/smart-rag/)
- [AutoRAG documentation](https://docs.auto-rag.com/)
- [RAGFlow](https://ragflow.io/)
- [pingcap/autoflow: pingcap/autoflow is a Graph RAG based and conversational knowledge base tool built with TiDB Serverless Vector Storage](https://github.com/pingcap/autoflow?tab=readme-ov-file)
- [kubemq-io/kubemq-graph-rag](https://github.com/kubemq-io/kubemq-graph-rag) via [The One Tool You Absolutely Need to Efficiently Scale Retrieval-Augmented Generation | HackerNoon](https://hackernoon.com/the-one-tool-you-absolutely-need-to-efficiently-scale-retrieval-augmented-generation)
- [ucbepic/docetl: A system for agentic LLM-powered data processing and ETL](https://github.com/ucbepic/docetl)
- [NirDiamant/Controllable-RAG-Agent](https://github.com/NirDiamant/Controllable-RAG-Agent)
- [superlinear-ai/raglite: 🥤 RAGLite is a Python toolkit for Retrieval-Augmented Generation (RAG) with PostgreSQL or SQLite](https://github.com/superlinear-ai/raglite)
- [foambubble/foam: A personal knowledge management and sharing system for VSCode](https://github.com/foambubble/foam)
- [lightonai/pylate: Late Interaction Models Training & Retrieval](https://github.com/lightonai/pylate)

- [theJayTea/WritingTools - system-wide grammar assistant](https://github.com/theJayTea/WritingTools)

## Papers

## annotation tools

- [HumanSignal/label-studio: Label Studio is a multi-type data labeling and annotation tool with standardized output format](https://github.com/HumanSignal/label-studio)
- [doccano/doccano: Open source annotation tool for machine learning practitioners.](https://github.com/doccano/doccano)
- [inception-project/inception: INCEpTION provides a semantic annotation platform offering intelligent annotation assistance and knowledge management.](https://github.com/inception-project/inception)
- [Termboard - Knowledge Graphs Made Simple](https://termboard.com/) - ontology designer

## graphrag

- [nemegrod/graph_RAG: Simple Graph RAG demo based on Jaguar data](https://github.com/nemegrod/graph_RAG) — graphdb, SPARQL, RDFS/OWL
- [stair-lab/kg-gen: [NeurIPS '25] Knowledge Graph Generation from Any Text](https://github.com/stair-lab/kg-gen)
- [microsoft/graphrag: A modular graph-based Retrieval-Augmented Generation (RAG) system](https://github.com/microsoft/graphrag)
- [1st1/lat.md: Agent Lattice: a knowledge graph for your codebase, written in markdown.](https://github.com/1st1/lat.md)
