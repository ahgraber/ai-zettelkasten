# Resources & References

[Introduction to the Zettelkasten Method- Zettelkasten Method](https://zettelkasten.de/introduction/)

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

## chunk

[chonkie-ai/chonkie: 🦛 CHONK your texts with Chonkie ✨ - The no-nonsense RAG chunking library](https://github.com/chonkie-ai/chonkie)
[[2312.06648] Dense X Retrieval: What Retrieval Granularity Should We Use?](https://arxiv.org/abs/2312.06648) -
deconstruct text blobs into propositions (complete factoids)

## index

- [LlamaIndex - LlamaIndex](https://docs.llamaindex.ai/en/stable/)
- [Introducing the Semantic Graph](https://neuml.hashnode.dev/introducing-the-semantic-graph)
- [TrustGraph | TrustGraph](https://trustgraph.ai/docs/TrustGraph)

## serve

- [Cinnamon/kotaemon: An open-source RAG-based tool for chatting with your documents.](https://github.com/Cinnamon/kotaemon)
- [CopilotKit/CopilotKit: React UI + elegant infrastructure for AI Copilots, in-app AI agents, AI chatbots, and AI-powered Textareas 🪁](https://github.com/CopilotKit/CopilotKit)

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

annotation tools

- [HumanSignal/label-studio: Label Studio is a multi-type data labeling and annotation tool with standardized output format](https://github.com/HumanSignal/label-studio)
- [doccano/doccano: Open source annotation tool for machine learning practitioners.](https://github.com/doccano/doccano)
- [inception-project/inception: INCEpTION provides a semantic annotation platform offering intelligent annotation assistance and knowledge management.](https://github.com/inception-project/inception)
