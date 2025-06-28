# 004 - Model Provider (Framework)

## Status

22 June 2025 - Proposed
28 June 20205 - Updated

<!-- Proposed/Accepted/Revised/Deprecated/Superseded -->

## Context

Given the speed of advancement and change of frontier generative AI model capabilities, I want a convenient way to change my model (and therefore model provider). Unfortunately, there is no standardized API for interacting with language model services. Many providers support a (limited) portion of OpenAI's `chat completions` API standard, but this is likely to induce errors due to incompatibility or API misalignment.

Packages like [BerriAI/litellm](https://github.com/BerriAI/litellm) and [andrewyng/aisuite:](https://github.com/andrewyng/aisuite) (and to some degree [simonw/llm](https://github.com/simonw/llm)) act as compatibility layers. Many frameworks ([langchain-ai/langchain](https://github.com/langchain-ai/langchain), [run-llama/llama_index](https://github.com/run-llama/llama_index), [pydantic/pydantic-ai](https://github.com/pydantic/pydantic-ai)) also provide a provider-agnostic abstraction layer.

Alternatively, services like [OpenRouter](https://openrouter.ai/) provide a unified API for a variety of services, providing access to multiple different AI models and providers through a single endpoint.

## Decision

### Selected Approach: **DIY with stubs and OpenAI defaults**

AI Zettelkasten requires simple primitives (instruction-following call/response and embedding). Most providers have some level of OpenAI compatible endpoint, so `embed`, `chat`, and async/batch variants cover most of the needs.

When more templated tool-calling or agentic use cases are called for, use `pydantic-ai`.

### Alternative Considered

#### Option 1: [BerriAI/litellm](https://github.com/BerriAI/litellm)

[BerriAI/litellm](https://github.com/BerriAI/litellm) is a Python SDK that standardizes the APIs for a variety of LLM providers to an OpenAI-compatible API format. It also provides a proxy server (LLM Gateway) for centralized LLM access management. While the proxy server is not needed, `litellm` allows an abstraction over multiple inference providers.

`litellm` would allow AI Zettelkasten to use any `litellm`-supported model/provider, allowing end-user flexibility.

Unfortunately, `litellm` is a dumpsterfire to hack against.

#### Option 2: **[OpenRouter](https://openrouter.ai/)**

[OpenRouter](https://openrouter.ai/) provides a unified (OpenAI-compatible) API for a variety of services through a single endpoint. This reduces the dependency and service account requirements for the project while still providing access to multiple different AI models from different providers. Further, OpenRouter can be used as a provider with `litellm`.

Unfortunately, _as of June 2025, OpenRouter does not provide embedding models_; an alternative service will have to be used.

#### Option 3: **OpenAI-compliant only**

Build only against the OpenAI client or OpenAI API. As most providers have an "OpenAI-compatible" endpoint (including Anthropic, Gemini, OpenRouter, Ollama, LMStudio, etc.), this is actually somewhat viable. However, the multi-compatibility is only surface level, providers may not support all features of the OpenAI API spec, and may not support the new Responses API.

#### Option 4: **[andrewyng/aisuite](https://github.com/andrewyng/aisuite)** / **[simonw/llm](https://github.com/simonw/llm)**

`aisuite` is intended to be a simpler alternative to `litellm`; however, they are not feature-equivalent and the development speed of `aisuite` is quite slow.

`llm` is a CLI tool and Python library for interacting with LLM providers. It supports most major LLM and embedding providers through a community-supported plugin system. However, it is primarily designed to be used in the CLI.

#### Option 5: AI Frameworks

Frameworks ([langchain-ai/langchain](https://github.com/langchain-ai/langchain), [run-llama/llama_index](https://github.com/run-llama/llama_index), [pydantic/pydantic-ai](https://github.com/pydantic/pydantic-ai)) also provide a provider-agnostic abstraction layer. They're also overkill for simple use as an abstraction layer over model providers.

## Implementation Details

<!-- Technical specifications
Required resources
Estimated timeline
Key implementation steps -->

## Related ADRs

- [005 - Chunking](./005-chunking.md)
- [006 - Embedding](./006-embedding.md)
- [007 - Indexing, Search, Retrieval](./007-index-search-retrieval.md)

## Additional Notes

<!-- Any supplementary information
References to documentation
Contact person for further questions -->
