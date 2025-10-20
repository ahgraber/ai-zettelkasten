# 002 - Content Parsing

## Status

21 June 2025 - Accepted

<!-- Proposed/Accepted/Revised/Deprecated/Superseded -->

## Context

Once content has been [scraped/downloaded](./001-content-archiving.md), it must be processed into standardized (markdown) text for further processing. The ideal solution will represent any document in well-structured markdown, including:

- parse complex document with multicolumn and/or interleaved text/table/chart/image content
- converting/extracting tabular data as markdown or HTML tables
- converting mathematical expressions and formulas to inline LaTeX
- caption charts, diagrams, and images with descriptive text
- extract charts, diagrams, and images into distinct files
- support using remote models through APIs (e.g., OpenAI, Anthropic, Gemini, OpenRouter)

## Decision

### Selected Approach: **[docling-project/docling](https://github.com/docling-project/docling/tree/main)**

### Rationale

[docling-project/docling](https://github.com/docling-project/docling) is a python-native document processing framework that is capable of parsing multiple document formats (PDF, HTML, image, etc.) into markdown, HTML, or JSON. It has broad support for integration with other AI frameworks, and is well-maintained and continuously developed. It provides OCR and can run local or remote vision models: for example its pipeline can generate descriptive captions for images ("picture description") using local VLMs or remote API calls. It also has enrichment steps: e.g. a formula model that converts detected equations into LaTeX. Docling can save extracted figures as image files (via generate_picture_images), and by default can produce text output in reading order (even for multi-column layouts).

Docling is an open source (MIT-licensed) project, supported by IBM.

Specialized extractors like [Gitingest](https://gitingest.com/) or [DeepWiki](https://docs.devin.ai/work-with-devin/deepwiki) might be useful for intuiting the intention behind GitHub repositories without actually fully processing the codebases.

### Alternative Considered

#### Option 1: Zerox

[getomni-ai/zerox](https://github.com/getomni-ai/zerox) is an open-source framework that leverages vanilla VLMs (via `LiteLLM`), providing an asynchronous API that performs OCR (Optical Character Recognition) to markdown conversion. From their docs:

1. Pass in a file (PDF, DOCX, image, etc.)
2. Convert that file into a series of images
3. Pass each image to a VLM and ask nicely for Markdown
4. Aggregate the responses and return Markdown

Given this, it may be more effective to fork or copypasta their approach and customize for `ai-zettelkasten`.

#### Option 2: MinerU/Dolphin

These are open-source document-parsing pipelines that use document-layout models and then extract text with a custom VLM. While the projects are open source and models are open-weights, the frameworks only work with their custom fine-tuned VLMs. These models are small (1-3B), but still require hosting (and the associated CPU/RAM/GPU capability).

[bytedance/Dolphin](https://github.com/bytedance/Dolphin) treats each page as an image and uses a two-stage VLM approach: first analyze page layout, then parse elements in parallel. Dolphin's page-level mode outputs a structured JSON (plus a Markdown version) representing the entire page in reading order and offers element-level parsing of individual tables, formulas or text blocks. In practice, Dolphin is a single pretrained model (available via Huggingface) that runs locally (requiring a GPU for inference). It excels at extracting text, tables, and formulas from document images into structured output, but it does not generate captions or separately extract embedded images. Dolphin is MIT-licensed and Python-based, but customization is limited to the provided model.

[opendatalab/MinerU](https://github.com/opendatalab/MinerU) produces plain text in human-readable order (handling single- and multi-column pages), preserving structure (headings, lists, etc.) and removing clutter (headers, footers). MinerU explicitly extracts images and their associated captions, tables (including table titles/footnotes), and recognizes formulas. Its pipeline converts equations to LaTeX and tables into HTML-formatted tables. MinerU supports OCR for both normal and garbled/scanned PDFs, and can run on CPU or GPU (with options for various backends, including a local VLM accelerator via SGLang).

#### Option 3: Unstructured / LlamaParse

- [Unstructured | Get your data LLM-ready.](https://unstructured.io/)
- [LlamaParse: Transform unstructured data into LLM optimized formats | LlamaIndex](https://www.llamaindex.ai/llamaparse)

Unstructured and LlamaParse are paid services accessible via APIs. I'd like to avoid reliance on external services beyond model providers.

#### Option 4: Nanonets OCR 2 / Docstrange

[Nanonets-OCR2](https://huggingface.co/nanonets/Nanonets-OCR2-3B) is a family of open-weight vision-language models (e.g., 3B, 1.5B) fine-tuned for document understanding and image-to-markdown conversion with semantic tagging. It outputs LLM-ready structured markdown including: HTML tables, LaTeX for equations, descriptive `<img>` tags for figures, and tags for signatures `<signature>`, watermarks `<watermark>`, and page numbers `<page_number>`. It can also handle checkboxes (standardized to Unicode), extract flow/organizational charts as Mermaid, and supports multilingual documents and VQA. See the overview post for details and evaluations: [Nanonets OCR 2](https://nanonets.com/research/nanonets-ocr-2/).

Access options include running locally via Transformers or vLLM using the Hugging Face weights, or using the hosted UI/API via [Docstrange – AI Document Data Extraction by Nanonets](https://docstrange.nanonets.com/). The hosted service advertises a generous free tier for experimentation. Using Docstrange introduces an external dependency; running locally reduces that dependency but requires hosting a VLM (likely GPU for best performance). The models' strong semantic formatting could reduce the need for downstream enrichment steps but may still benefit from a unifying pipeline (e.g., docling) for consistency across sources.

## Implementation Details

<!-- Technical specifications
Required resources
Estimated timeline
Key implementation steps -->

## Related ADRs

- [001 - Content Archiving](./001-content-archiving.md)
- [003 - Database](./003-database.md)
- [005 - Chunking](./005-chunking.md)

## Additional Notes

Tools:

- [docling-project/docling: Get your documents ready for gen AI](https://github.com/docling-project/docling/tree/main)
- [bytedance/Dolphin: "Dolphin: Document Image Parsing via Heterogeneous Anchor Prompting", ACL, 2025.](https://github.com/bytedance/Dolphin)
- [opendatalab/MinerU: A high-quality tool for convert PDF to Markdown and JSON.一站式开源高质量数据提取工具，将PDF转换成Markdown和JSON格式。](https://github.com/opendatalab/MinerU/tree/master)
- [VikParuchuri/marker: Convert PDF to markdown + JSON quickly with high accuracy](https://github.com/VikParuchuri/marker)
- [getomni-ai/zerox: PDF to Markdown with vision models](https://github.com/getomni-ai/zerox)
- [mindee/doctr: docTR (Document Text Recognition) - a seamless, high-performing & accessible library for OCR-related tasks powered by Deep Learning.](https://github.com/mindee/doctr)
- [microsoft/markitdown: Python tool for converting files and office documents to Markdown.](https://github.com/microsoft/markitdown)
  (doesn't do pdf tables very well)
- [nanonets/Nanonets-OCR-s · Hugging Face](https://huggingface.co/nanonets/Nanonets-OCR-s)
- [Unstructured - Open Source](https://docs.unstructured.io/open-source/introduction/overview)
- [morphik-org/morphik-core: Open source multi-modal RAG for building AI apps over private knowledge.](https://github.com/morphik-org/morphik-core)
- [LlamaParse: Transform unstructured data into LLM optimized formats — LlamaIndex - Build Knowledge Assistants over your Enterprise Data](https://www.llamaindex.ai/llamaparse)
- [LLMWhisperer: Make Complex Document Data Ready for LLMs](https://unstract.com/llmwhisperer/)
- [Jina Reader API](https://jina.ai/reader/) or [Jina reader-lm-1.5b - Search Foundation Models](https://jina.ai/models/reader-lm-1.5b)

Benchmarks:

- [[2412.02592] OCR Hinders RAG: Evaluating the Cascading Impact of OCR on Retrieval-Augmented Generation](https://arxiv.org/abs/2412.02592)
- [[2412.07626] OmniDocBench: Benchmarking Diverse PDF Document Parsing with Comprehensive Annotations](https://arxiv.org/abs/2412.07626)
- [[2501.00321] OCRBench v2: An Improved Benchmark for Evaluating Large Multimodal Models on Visual Text Localization and Reasoning](https://arxiv.org/abs/2501.00321)
- [Intelligent Document Processing Leaderboard](https://idp-leaderboard.org/)
- [getomni-ai/benchmark: OCR Benchmark](https://github.com/getomni-ai/benchmark)
