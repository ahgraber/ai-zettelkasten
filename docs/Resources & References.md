# Resources & References

[Introduction to the Zettelkasten Method- Zettelkasten Method](https://zettelkasten.de/introduction/)

[From Vector Search to Entity Processing: Evolving Zettelgarden's Connection Engine - Zettelgarden](https://zettelgarden.com/blog/entities)
and [HN Discussion](https://news.ycombinator.com/item?id=42577387)

## ingest

- [ArchiveBox | 🗃 Open source self-hosted web archiving. Takes URLs/browser history/bookmarks/Pocket/Pinboard/etc., saves HTML, JS, PDFs, media, and more…](https://archivebox.io/)

  - [docker-archivebox/archivebox.yml at main · ArchiveBox/docker-archivebox](https://github.com/ArchiveBox/docker-archivebox/blob/main/archivebox.yml)
  - inputs via webui, browser extension, git repo
  - [Thoughts on using a git repo as a source for links to archive? · ArchiveBox/ArchiveBox · Discussion #1010](https://github.com/ArchiveBox/ArchiveBox/discussions/1010)
    - see
      [mendel5/alternative-front-ends: Overview of alternative open source front-ends for popular internet platforms (e.g. YouTube, Twitter, etc.)](https://github.com/mendel5/alternative-front-ends)
      for alternative source sites

- webhook triggers next step (prefect to send docling run?)

  - archivebox saves to s3(?), use minio to send webhook notification? --> might be able to use archivebox to send the
    webhook notification instead of S3
  - [ArchiveBox's archive file layout structure](https://github.com/ArchiveBox/ArchiveBox?tab=readme-ov-file#archive-layout)
    - [Automating API Calls on New MinIO S3 Files - Claude](https://claude.ai/chat/1db7a147-4800-4005-acd4-e5eb7fe27f63)
    - Might be able to use archivebox itself to send the webhook -
      [Webhook notification for automation? · ArchiveBox/ArchiveBox · Discussion #1597](https://github.com/ArchiveBox/ArchiveBox/discussions/1597)

- [dgtlmoon/changedetection.io: The best and simplest free open source web page change detection, website watcher, restock monitor and notification service. Restock Monitor, change detection. Designed for simplicity - Simply monitor which websites had a text change for free. Free Open source web page change detection, Website defacement monitoring, Price change notification](https://github.com/dgtlmoon/changedetection.io/tree/master)

  - [dgtlmoon/pyppeteer-ng: Headless chrome/chromium automation library (unofficial port of puppeteer)](https://github.com/dgtlmoon/pyppeteer-ng/tree/dev?tab=readme-ov-file)

## parse

- [Optimize parsing costs with LlamaParse auto mode — LlamaIndex - Build Knowledge Assistants over your Enterprise Data](https://www.llamaindex.ai/blog/optimize-parsing-costs-with-llamaparse-auto-mode)
- [DS4SD/docling: Get your documents ready for gen AI](https://github.com/DS4SD/docling) and
  [DS4SD/docling-serve: Running Docling as an API service](https://github.com/DS4SD/docling-serve)
  - see also
    [Production deployment via HuggingFace? · DS4SD/docling · Discussion #227](https://github.com/DS4SD/docling/discussions/227)
- [getomni-ai/zerox: PDF to Markdown with vision models](https://github.com/getomni-ai/zerox)
- [VikParuchuri/marker: Convert PDF to markdown + JSON quickly with high accuracy](https://github.com/VikParuchuri/marker)
- [mindee/doctr: docTR (Document Text Recognition) - a seamless, high-performing & accessible library for OCR-related tasks powered by Deep Learning.](https://github.com/mindee/doctr)
- [Zotero | Your personal research assistant](https://www.zotero.org/)
  - [Zotero and RAG? - Zotero Forums](https://forums.zotero.org/discussion/110441/zotero-and-rag)
  - [urschrei/pyzotero: Pyzotero: a Python client for the Zotero API](https://github.com/urschrei/pyzotero)
  - [DIY: Ground LLaMa on your papers from Zotero | by Emmett McFarlane | Medium](https://medium.com/@emcf1/diy-ground-a-language-model-on-your-papers-from-zotero-with-finesse-a5c4ca7c187a)
- [emcf/thepipe: Extract clean data from anywhere, powered by vision-language models ⚡](https://github.com/emcf/thepipe)
- [microsoft/markitdown: Python tool for converting files and office documents to Markdown.](https://github.com/microsoft/markitdown)
- [cyclotruc/gitdigest: Web interface to turn codebases into prompt-friendly text](https://github.com/cyclotruc/gitdigest?tab=readme-ov-file)
- [[2408.15836] Knowledge Navigator: LLM-guided Browsing Framework for Exploratory Search in Scientific Literature](https://arxiv.org/abs/2408.15836)

## chunk

[chonkie-ai/chonkie: 🦛 CHONK your texts with Chonkie ✨ - The no-nonsense RAG chunking library](https://github.com/chonkie-ai/chonkie)
[[2312.06648] Dense X Retrieval: What Retrieval Granularity Should We Use?](https://arxiv.org/abs/2312.06648) -
deconstruct text blobs into propositions (complete factoids)

## index

- [PyLate](https://lightonai.github.io/pylate/)
- [LlamaIndex - LlamaIndex](https://docs.llamaindex.ai/en/stable/)
- [Introducing the Semantic Graph](https://neuml.hashnode.dev/introducing-the-semantic-graph)
- [TrustGraph | TrustGraph](https://trustgraph.ai/docs/TrustGraph)

### wiki

If building a concept graph, it would be useful to be able to use it as a human in addition to have it be searchable
for RAG.

- [Logseq: A privacy-first, open-source knowledge base](https://logseq.com/)
- [Obsidian - Sharpen your thinking](https://obsidian.md/)

### retrievers

- bm25
- Learned Sparse Retrieval
  - SPLADE
    - [naver/splade: SPLADE: sparse neural search (SIGIR21, SIGIR22)](https://github.com/naver/splade?tab=readme-ov-file)
    - [From grep to SPLADE: a journey through semantic search](https://blog.elicit.com/semantic-search/)
    - [SPLADE for Sparse Vector Search Explained | Pinecone](https://www.pinecone.io/learn/splade/)
  - TILDE -
    [[2108.08513] Fast Passage Re-ranking with Contextualized Exact Term Matching and Efficient Passage Expansion](https://arxiv.org/abs/2108.08513)
  - [Context-Aware Document Term Weighting for Ad-Hoc Search](https://dl.acm.org/doi/pdf/10.1145/3366423.3380258)
- vector

## serve

- [Cinnamon/kotaemon: An open-source RAG-based tool for chatting with your documents.](https://github.com/Cinnamon/kotaemon)
- [Chainlit/chainlit: Build Conversational AI in minutes ⚡️](https://github.com/Chainlit/chainlit)
- [CopilotKit/CopilotKit: React UI + elegant infrastructure for AI Copilots, in-app AI agents, AI chatbots, and AI-powered Textareas 🪁](https://github.com/CopilotKit/CopilotKit)
- [AnswerDotAI/fasthtml: The fastest way to create an HTML app](https://github.com/AnswerDotAI/fasthtml)
  - [MonsterUI: Bringing Beautiful UI to FastHTML – Answer.AI](https://www.answer.ai/posts/2025-01-15-monsterui.html)
- [reflex-dev/reflex: 🕸️ Web apps in pure Python 🐍](https://github.com/reflex-dev/reflex)

See also

- [Building AI Products—Part I: Back-end Architecture](https://philcalcado.com/2024/12/14/building-ai-products-part-i.html)

- [Introduction — Build, scale, and manage user-facing Retrieval-Augmented Generation applications.](https://r2r-docs.sciphi.ai/introduction)

- [Future-House/paper-qa: High accuracy RAG for answering questions from scientific documents with citations](https://github.com/Future-House/paper-qa)

- [prsdm/smart-rag](https://github.com/prsdm/smart-rag/)

- [AutoRAG documentation](https://docs.auto-rag.com/)

- [RAGFlow](https://ragflow.io/)

- [pingcap/autoflow: pingcap/autoflow is a Graph RAG based and conversational knowledge base tool built with TiDB Serverless Vector Storage](https://github.com/pingcap/autoflow?tab=readme-ov-file)

- [kubemq-io/kubemq-graph-rag](https://github.com/kubemq-io/kubemq-graph-rag) via
  [The One Tool You Absolutely Need to Efficiently Scale Retrieval-Augmented Generation | HackerNoon](https://hackernoon.com/the-one-tool-you-absolutely-need-to-efficiently-scale-retrieval-augmented-generation)

- [ucbepic/docetl: A system for agentic LLM-powered data processing and ETL](https://github.com/ucbepic/docetl)

- [NirDiamant/Controllable-RAG-Agent: This repository provides an advanced Retrieval-Augmented Generation (RAG) solution for complex question answering. It uses sophisticated graph based algorithm to handle the tasks.](https://github.com/NirDiamant/Controllable-RAG-Agent)

- [superlinear-ai/raglite: 🥤 RAGLite is a Python toolkit for Retrieval-Augmented Generation (RAG) with PostgreSQL or SQLite](https://github.com/superlinear-ai/raglite)

- [foambubble/foam: A personal knowledge management and sharing system for VSCode](https://github.com/foambubble/foam)

- [lightonai/pylate: Late Interaction Models Training & Retrieval](https://github.com/lightonai/pylate)

## plugins

- [theJayTea/WritingTools: The world's smartest system-wide grammar assistant; a better version of the Apple Intelligence Writing Tools. Works on Windows, Linux, & macOS, with the free Gemini API, local LLMs, & more.](https://github.com/theJayTea/WritingTools)

## annotation tools

- [HumanSignal/label-studio: Label Studio is a multi-type data labeling and annotation tool with standardized output format](https://github.com/HumanSignal/label-studio)

- [doccano/doccano: Open source annotation tool for machine learning practitioners.](https://github.com/doccano/doccano)

- [inception-project/inception: INCEpTION provides a semantic annotation platform offering intelligent annotation assistance and knowledge management.](https://github.com/inception-project/inception)

- [Termboard - Knowledge Graphs Made Simple](https://termboard.com/) - ontology designer

## graphrag

[nemegrod/graph_RAG: Simple Graph RAG demo based on Jaguar data](https://github.com/nemegrod/graph_RAG) - graphdb, SPARQL, RDFS/OWL
