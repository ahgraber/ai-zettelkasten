## ADDED Requirements

### Requirement: Trace upstream model calls

The system SHALL emit an MLflow trace span for each upstream LLM/AI model call made by conversion workers and shared batch utilities when tracing is enabled.

#### Scenario: Trace emitted for picture-description call

- **WHEN** a conversion job performs a picture-description upstream chat completion call
- **THEN** the system records a trace span with operation name, model identifier, provider endpoint type, start/end timing, and success status

#### Scenario: Trace emitted for batch model calls

- **WHEN** batch chat or batch embeddings processing is executed through the shared batch utility
- **THEN** the system records trace spans that identify batch operation type, model identifier, request count, duration, and final status

### Requirement: Span naming and type separation

The system SHALL use stable action-oriented span names and explicit MLflow span types to separate embedding and LLM traces for queryability.

#### Scenario: Embedding span classification

- **WHEN** an embedding operation is traced
- **THEN** the span uses `SpanType.EMBEDDING` and an embedding-specific operation name

#### Scenario: LLM span classification

- **WHEN** a chat completion or equivalent LLM generation operation is traced
- **THEN** the span uses an LLM-oriented span type and an LLM-specific operation name

### Requirement: Embedding trace attributes are minimal

The system MUST record only `model`, `latency`, and `status` as required attributes for embedding spans.

#### Scenario: Embedding attribute payload

- **WHEN** an embedding span is emitted
- **THEN** the required attributes include model identifier, duration/latency, and operation status and do not require additional GenAI payload fields

### Requirement: LLM tracing captures robust GenAI spans

The system SHALL capture robust LLM tracing spans that include essential GenAI execution context in addition to base timing/status data.

#### Scenario: LLM span context richness

- **WHEN** an LLM call span is emitted
- **THEN** it includes model identifier, timing, final status, and sanitized GenAI context needed for debugging (for example token usage and provider/request metadata when available)

### Requirement: Tracing is optional and non-disruptive

The system MUST preserve existing conversion behavior when MLflow tracing is disabled, misconfigured, or temporarily unavailable.

#### Scenario: Tracing disabled

- **WHEN** tracing configuration is absent or disabled
- **THEN** model calls execute normally and no trace emission is attempted

#### Scenario: Tracing backend failure

- **WHEN** MLflow trace emission raises an exception during a model call
- **THEN** the model call outcome is preserved and the tracing failure is handled without crashing the conversion flow

### Requirement: Trace metadata is sanitized

The system MUST avoid emitting secrets and raw sensitive payload content in MLflow trace attributes.

#### Scenario: API credential protection

- **WHEN** tracing metadata is generated for an upstream model call
- **THEN** API keys and authorization headers are never included in trace attributes

#### Scenario: Payload minimization

- **WHEN** text or document content is sent to upstream model endpoints
- **THEN** traces include only safe summary fields (for example counts, sizes, or identifiers) rather than full raw content
