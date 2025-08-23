# 005 - Chunking

## Status

22 June 2025 - Proposed

<!-- Proposed/Accepted/Revised/Deprecated/Superseded -->

## Context

Language models have limitations:

1. Models have limited context (even if it is increasing gen-over-gen), so we should be careful to not overload it
2. Token processing is expensive, so we don't want to pay for tokens used that aren't useful
3. Models are distracted by irrelevant context, so we only want to send the most relevant information

Chunking is the process of splitting long documents into shorter sections. Ideally, we want to define the chunks such that they represent granular concepts or facts. Chunking overlaps with the Zettelkasten concept of _atomicity_ in which each Node ("zettel", from German "slip" or "note") contains only a single concept, idea, or fact.

There are a variety of chunking strategies and frameworks that support chunking.

## Decision

### Selected Approach: Chunking as side-effect of extraction

Since the idea behind chunking is to break the document down into atomic, self-coherent subsections, we use guided extraction to extract _knowledge representations_ (based loosely on Bloom's Taxonomy) from the document beyond naive text splitting.

- **Facts**: statements or assertions that can be objectively verified as true or false based on empirical evidence or reality
- **Evaluations**: reasoned judgments or assessments based on explicit criteria, standards, or values that determine quality, effectiveness, or worth
  - **Judgments**: evidence-based assessments using explicit criteria
  - **Beliefs**: Accepted propositions that may lack empirical verification but are held as true
  - **Opinions**: Personal viewpoints or preferences not necessarily based on objective criteria
- **Procedures**: Structured sequences of actions, steps, or operations designed to achieve a specific outcome or solve a particular problem, grounded in practice
- **Concepts**: Abstract mental constructs that capture the essential meaning, characteristics, or principles underlying phenomena, enabling comprehension and interpretation

### Rationale

[Claimify](https://www.microsoft.com/en-us/research/blog/claimify-extracting-high-quality-claims-from-language-model-outputs/) is an LLM-based method to extract claims such that:

1. The claims should capture all verifiable content in the source text and exclude unverifiable content.
2. Each claim should be entailed (i.e., fully supported) by the source text.
3. Each claim should be understandable on its own, without additional context.
4. Each claim should minimize the risk of excluding critical context.
5. The system should flag cases where ambiguity cannot be resolved.

_Claims_ look very similar to the _atomic concepts_ used in Zettelkasten! Instead of using a more naive chunking strategy, we can use Claimify to extract atomic claims for use as chunks. `Claimify` focuses on extracting "verifiable factual claims", or "statements or assertions that can be objectively verified as true or false based on empirical evidence or reality." In practice, our implementation must be more flexible; we do not want to exclude facts on the grounds of empiricism when the Zettelkasten may benefit from unverifiable thought, opinion, or conjecture.

Read more: [[2502.10855] Towards Effective Extraction and Evaluation of Factual Claims](https://arxiv.org/abs/2502.10855)

### Alternative Considered

#### Option 1: **[chonkie-inc/chonkie](https://github.com/chonkie-inc/chonkie)**

[chonkie](https://github.com/chonkie-inc/chonkie) is a chunking framework that provides a variety of approaches for chunking larger documents, including naive methods like chunking by token count or sentence boundaries, semantic methods that identify breaks between concepts, or more complex agentic approaches.

## Implementation Details

### Claimify

From [[2502.10855] Towards Effective Extraction and Evaluation of Factual Claims](https://arxiv.org/abs/2502.10855):

#### 1. Context Creation

Claimify accepts a question-answer pair as input. It uses NLTK's `sentence tokenizer` to split the answer into sentences. Context is created for each sentence _s_ based on a configurable combination of _p_ preceding sentences, _f_ following sentences, and optional metadata (e.g., the header hierarchy in a Markdownstyle answer).

The parameters _p_ and _f_ can be defined separately for the subsequent stages, allowing each stage to have a distinct context. In practice, _p_ was set to 5 for all stages, and _f_ was set to 5 for selection and 0 for disambiguation and decomposition.

#### 2. Selection

Next, Claimify uses an LLM to determine whether each sentence contains any verifiable content, in light of its context and the question. When the LLM identifies that a sentence contains both verifiable and unverifiable components, it rewrites the sentence, retaining only the verifiable components.

More specifically, the LLM selects one of the following options:

1. state that the sentence does not contain any verifiable content,
2. return a modified version of the sentence that retains only verifiable content, or
3. return the original sentence, indicating that it does not contain any unverifiable content.

If the LLM selects the first option, the sentence is labeled "No verifiable claims" and excluded from subsequent stages (disambiguation, decomposition).

#### 3. Disambiguation

Claimify uses an LLM to identify two types of ambiguity. The first is _referential ambiguity_, which occurs when it is unclear what a word or phrase refers to. The second is _structural ambiguity_, which occurs when grammatical structure allows for multiple interpretations.

The LLM is also asked to determine whether each instance of ambiguity can be resolved using the question and the context and selects one of the following options:

1. state the sentence "Cannot be disambiguated",
2. return a clarified version of the sentence, resolving all ambiguity, or
3. returns the original sentence, indicating there was no ambiguity.

If the LLM selects the first option, the sentence and excluded from the Decomposition stage, even if it has unambiguous, verifiable components.

#### 4. Decomposition

Finally, Claimify uses an LLM to decompose each disambiguated sentence into decontextualized factual claims. If it does not return any
claims, the sentence is labeled "No verifiable claims."

Extracted claims may include text in brackets, which typically represents information implied by the question or context but not explicitly stated in the source sentence. A benefit of bracketing is that it flags inferred content, which is inherently less reliable than content
explicitly stated in the source sentence

### Agentic Extraction

Since context management is key, some level of document-structure pre-chunking (i.e., split a document by its markdown headings) will be used to allow the LLM to extract claims over more manageable pieces of text. However, this creates a different problem -- disambiguation over paragraph or section boundaries.

Claimify solved this by passing _p_ preceding and _f_ following sentences along with the sentence under analysis.

By implementing a Claimify-like process as an agentic system, we could alternatively provide the claim extraction Agent tools that allow it to search for terms over the complete document for disambiguation without overloading the context window of the initial request.

The resulting chunks will be extracts of a somewhat broader classification that Claimify's facts, and each chunk should contain its Extract (claim), Representation Type, and Source Context.

<!-- Technical specifications
Required resources
Estimated timeline
Key implementation steps -->

## Related ADRs

- [004 - Model Provider (Framework)](./004-model-provider.md)
- [006 - Embedding](./006-embedding.md)
- [007 - Indexing, Search, Retrieval](./007-index-search-retrieval.md)

## Additional Notes

- [Contextual Retrieval \\ Anthropic](https://www.anthropic.com/news/contextual-retrieval)
- [Late Chunking in Long-Context Embedding Models](https://jina.ai/news/late-chunking-in-long-context-embedding-models/)
- [Decoupled chunk representations](https://docs.llamaindex.ai/en/stable/optimizing/production_rag/#decoupling-chunks-used-for-retrieval-vs-chunks-used-for-synthesis): 'retrieval chunks' from 'synthesis chunks' - the chunk used for embedding representation may be different from the chunk sent to the LM for response generation
