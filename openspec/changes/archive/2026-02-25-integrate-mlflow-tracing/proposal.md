## Why

Upstream LLM/model calls currently run without centralized request/response tracing, which makes debugging failures, latency spikes, and model behavior regressions slow and manual.
We need MLflow tracing support now so conversion runs can be inspected with consistent, queryable telemetry as AI usage grows.

## What Changes

- Add MLflow `>=3.9` as a runtime dependency and define environment-driven tracing configuration for conversion service processes.
- Instrument upstream model-call boundaries in the conversion pipeline (chat-completions-based picture description and OpenAI batch chat/embeddings workflows).
- Ensure traces capture request metadata, timing, outcome status, and sanitized error information without leaking secrets or raw sensitive payloads.
- Add test coverage for trace emission and for behavior when tracing is disabled or MLflow is unreachable.

## Capabilities

### New Capabilities

- `mlflow-llm-tracing`: Emit MLflow traces for upstream LLM/AI model calls made by conversion workers and shared batch utilities.

### Modified Capabilities

- None.

## Impact

- Affected code:
  - `src/aizk/conversion/workers/converter.py` (picture-description chat completion integration point)
  - `src/aizk/utilities/batch_utils.py` (OpenAI batch chat/embeddings integration point)
  - `src/aizk/conversion/utilities/config.py` and process startup paths for tracing configuration
- Dependencies: add `mlflow>=3.9`.
- Operations: requires MLflow tracking configuration via environment variables in worker/API runtimes.
