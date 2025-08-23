# 006 - Embedding

## Status

22 June 2025 - Proposed

<!-- Proposed/Accepted/Revised/Deprecated/Superseded -->

## Context

<!-- What is the problem or challenge we're addressing?
What are the existing constraints?
Why does this decision matter? -->

|                         Model | $ / 1M text tokens | $ / 1M image tokens | context length  |
| ----------------------------: | ------------------ | ------------------- | --------------- |
| openai text-embedding-3-small | $0.02              | -                   | 8k              |
| openai text-embedding-3-large | $0.13              | -                   | 8k              |
|                cohere-embed-4 | $0.12              | $0.47               | 128k            |
|                    voyage-3.5 | $0.06              | -                   | 32k             |
|               voyage-3.5-lite | $0.02              | -                   | 32k             |
|           voyage-multimodal-3 | $0.12              | $0.60\*             | 32k             |
|            jina-embeddings-v3 | $0.05              | -                   | 8k              |
|                  jina-clip-v2 | $0.05              | $0.05               | 8k / 512x512 px |

> Voyage: Every 560 pixels counts as a token.
> \* $0.60 per **_1B pixels_**
>
> Jina: each 512x512 pixel tiles costs 4,000 tokens to process, including partially filled tiles. _For optimal cost-efficiency, we recommend that API users resize their images to 512x512 before sending requests._

- [OpenAI text-embedding-3-small](https://platform.openai.com/docs/models/text-embedding-3-small) or [OpenAI text-embedding-3-large](https://platform.openai.com/docs/models/text-embedding-3-large)
- Cohere provides [Embed](https://cohere.com/embed), multilingual, multimodal embedding models, and [Rerank](https://cohere.com/rerank) a strong multilingual (but text-only) reranking model.
- [VoyageAI Text Embeddings](https://docs.voyageai.com/docs/embeddings) are among the most performant; they are also expensive
- [Jina Embedding API](https://jina.ai/embeddings/) support [Late Chunking in Long-Context Embedding Models](https://jina.ai/news/late-chunking-in-long-context-embedding-models/), which are an intriguing concept.

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

#### Option 2: [Alternative Approach Name]

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

- [004 - Model Provider (Framework)](./004-model-provider.md)
- [005 - Chunking](./005-chunking.md)
- [007- Indexing, Search, Retrieval](./007-index-search-retrieval.md)

## Additional Notes

<!-- Any supplementary information
References to documentation
Contact person for further questions -->
