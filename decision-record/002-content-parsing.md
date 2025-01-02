# 002 - Content Parsing

## Status

<!-- Date -->
<!-- Proposed/Accepted/Deprecated/Superseded -->

## Context

<!-- What is the problem or challenge we're addressing?
What are the existing constraints?
Why does this decision matter? -->

How do we process scraped/downloaded documents into standardized (markdown?) text for chunking, etc.?

Self-hosted:

- [DS4SD/docling: Get your documents ready for gen AI](https://github.com/DS4SD/docling/tree/main)
- [VikParuchuri/marker: Convert PDF to markdown + JSON quickly with high accuracy](https://github.com/VikParuchuri/marker)
- [opendatalab/MinerU: A high-quality tool for convert PDF to Markdown and JSON.一站式开源高质量数据提取工具，将PDF转换成Markdown和JSON格式。](https://github.com/opendatalab/MinerU/tree/master)
- [getomni-ai/zerox: PDF to Markdown with vision models](https://github.com/getomni-ai/zerox)
- [mindee/doctr: docTR (Document Text Recognition) - a seamless, high-performing & accessible library for OCR-related tasks powered by Deep Learning.](https://github.com/mindee/doctr)
- [microsoft/markitdown: Python tool for converting files and office documents to Markdown.](https://github.com/microsoft/markitdown)
  (doesn't do pdf tables very well)
- [Jina reader-lm-1.5b - Search Foundation Models](https://jina.ai/models/reader-lm-1.5b)

Service:

- [Pricing and usage data | LlamaCloud Documentation](https://docs.cloud.llamaindex.ai/llamaparse/usage_data)
- [LLMWhisperer: Make Complex Document Data Ready for LLMs](https://unstract.com/llmwhisperer/)
- [Jina Reader API](https://jina.ai/reader/)

Benchmarks:

- [[2412.02592] OCR Hinders RAG: Evaluating the Cascading Impact of OCR on Retrieval-Augmented Generation](https://arxiv.org/abs/2412.02592)

## Decision

### Selected Approach

<!-- What solution are we selecting?
Provide a clear, concise description of the chosen approach -->

### Rationale

<!-- Why was this specific approach selected?
What alternative options were considered?
What are the key benefits of this decision? -->

### Consequences

#### Positive Impacts

<!-- Potential advantages of the decision
Short-term and long-term benefits -->

#### Potential Risks

<!-- Possible negative consequences
Potential challenges or limitations -->

#### Mitigation Strategies

<!-- How will we address potential risks?
Contingency plans or monitoring approaches -->

### Alternative Considered

#### Option 1: [Alternative Approach Name]

<!-- Description
Pros
Cons
Reason for not selecting -->

### Option 2: [Alternative Approach Name]

<!-- Description
Pros
Cons
Reason for not selecting -->

## Implementation Details

<!-- Technical specifications
Required resources
Estimated timeline
Key implementation steps -->

## Related ADRs

<!-- Reference numbers of related decisions
Links to dependent or impacted architectural decisions -->

## Additional Notes

<!-- Any supplementary information
References to documentation
Contact person for further questions -->
