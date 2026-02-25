## Context

The conversion service performs upstream AI calls in at least two high-value paths: picture-description chat completion requests in `converter.py` and OpenAI batch chat/embedding requests in `batch_utils.py`.
Today these calls rely on local logs and exception paths for visibility, which is insufficient for correlating call latency, provider failures, and model-level regressions across jobs.

This change introduces MLflow tracing (`>=3.9`) as a shared observability layer for those call boundaries.
The runtime is environment-configured and may execute with tracing disabled in some environments.

## Goals / Non-Goals

**Goals:**

- Emit consistent MLflow traces around upstream LLM/model calls in the conversion worker path.
- Capture core metadata for debugging: provider endpoint type, model name, timing, status, and normalized error details.
- Keep tracing optional and non-disruptive so conversion behavior remains unchanged when tracing is disabled or backend is unavailable.
- Avoid logging secrets and raw sensitive request payloads in trace metadata.

**Non-Goals:**

- Instrument every function in the conversion service.
- Replace existing structured logs or error handling.
- Introduce new product-facing APIs for trace access.

## Decisions

1. Add a small tracing adapter module rather than calling MLflow APIs inline everywhere.

- Rationale: centralizes configuration, allows no-op behavior when disabled, and keeps call sites small.
- Alternative considered: inline `mlflow` calls in each integration point.
  Rejected due to repeated guard logic and harder testing.

2. Instrument only external model-call boundaries in this change.

- Rationale: this is where observability value is highest and where external failures/latency occur.
- Alternative considered: broad end-to-end run tracing.
  Deferred to keep implementation focused and low-risk.

3. Use best-effort trace emission with failure isolation.

- Rationale: observability must not break conversion jobs; MLflow outages should degrade to warning logs.
- Alternative considered: fail-fast when tracing backend fails.
  Rejected because it creates an availability dependency on telemetry.

4. Record sanitized metadata only.

- Rationale: API keys and raw content may include sensitive text; traces should include identifiers and sizes, not secrets.
- Alternative considered: full payload capture.
  Rejected for security and data-minimization reasons.

5. Separate embedding and LLM tracing using both span type and stable span names.

- Rationale: MLflow trace search supports filtering by span name and span type, so separation should be explicit in both dimensions for reliable querying.
- Decision detail:
  - Embedding calls use `SpanType.EMBEDDING` with minimal attributes: `model`, `latency_ms`, and `status`.
  - LLM/chat calls use LLM-oriented span types (`SpanType.CHAT_MODEL`/equivalent supported MLflow LLM span type) with richer GenAI span attributes.
  - Span names are action-oriented and stable (for example `embedding.batch` and `llm.chat.completions`) rather than dynamic/user-derived values.
- Alternative considered: one generic span name (`upstream_model_call`) plus `call_type` attribute.
  Rejected because it weakens discoverability and default filtering in MLflow.

6. Defer trace sampling controls.

- Rationale: current scope is correctness and observability baseline; sampling policy can be introduced after real trace volume is measured.
- Alternative considered: adding sampling controls now.
  Rejected to avoid premature complexity.

## Risks / Trade-offs

- [Risk] Trace wrappers could add small latency overhead. -> Mitigation: keep metadata minimal and avoid expensive serialization.
- [Risk] Missing/incorrect correlation keys reduce trace usefulness. -> Mitigation: standardize span attributes and validate in tests.
- [Risk] Divergence between direct chat path and batch path instrumentation. -> Mitigation: shared adapter and common attribute naming conventions.
- [Risk] MLflow connectivity failures could generate noisy logs. -> Mitigation: bounded warning logs and graceful no-op fallback.

## Migration Plan

1. Add `mlflow>=3.9` dependency and tracing config fields.
2. Implement tracing adapter with enable/disable logic and error-isolated emission.
3. Instrument `converter.py` picture-description request path and `batch_utils.py` chat/embeddings batch execution path.
4. Ship with tracing disabled by default unless explicit env settings are present.
5. Rollback strategy: disable via env flag or remove MLflow config values; keep conversion code paths operational.

## Open Questions

- No open questions at this time.
- Follow-up work (deferred): introduce sampling controls once production trace volume is observed.
