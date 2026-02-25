## 1. Dependency and configuration

- [x] 1.1 Add `mlflow>=3.9` to project dependencies.
- [x] 1.2 Add tracing config fields to conversion settings (enable flag and MLflow tracking identifiers/endpoints as needed).
- [x] 1.3 Document required/optional tracing environment variables in `.env.example` and conversion docs.

## 2. Tracing adapter

- [x] 2.1 Implement a small MLflow tracing adapter module with no-op behavior when disabled.
- [x] 2.2 Add a sanitized metadata helper that strips secrets and raw payload text before emitting attributes.
- [x] 2.3 Ensure adapter failures are isolated (log warning, do not alter model-call success/failure semantics).

## 3. Instrument upstream call sites

- [x] 3.1 Instrument `src/aizk/conversion/workers/converter.py` picture-description chat completion boundary with trace spans.
- [x] 3.2 Instrument `src/aizk/utilities/batch_utils.py` chat and embeddings batch processing boundaries with trace spans.
- [x] 3.3 Use explicit span-type separation and stable names (`SpanType.EMBEDDING` + embedding operation name, LLM span type + LLM operation name).
- [x] 3.4 Enforce embedding span attribute minimum (`model`, `latency`, `status`) only.
- [x] 3.5 Add richer sanitized GenAI attributes for LLM spans (token usage and provider/request metadata when available).

## 4. Verification

- [x] 4.1 Add tests covering trace emission for picture-description and batch call paths when tracing is enabled.
- [x] 4.2 Add tests covering disabled tracing and MLflow emission failures to confirm non-disruptive behavior.
- [x] 4.3 Add tests asserting span naming and type separation between embeddings and LLM calls.
- [x] 4.4 Add tests asserting embedding spans only require `model`, `latency`, and `status`.
- [x] 4.5 Run project lint and targeted tests for updated modules.
