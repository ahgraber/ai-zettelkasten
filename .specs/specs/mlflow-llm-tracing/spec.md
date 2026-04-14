# MLflow LLM Tracing Specification

> Translated from OpenSpec on 2026-03-21
> Source: openspec/specs/mlflow-llm-tracing/spec.md

## Purpose

This capability instruments upstream LLM and embedding model calls made by conversion workers with MLflow trace spans.
It provides observability into model invocations — including timing, status, and sanitized metadata — while remaining entirely non-disruptive to the conversion pipeline when tracing is disabled or unavailable.

## Requirements

### Requirement: Trace upstream model calls

The system SHALL emit an MLflow trace span for each upstream LLM/AI model call made by conversion workers and shared batch utilities when tracing is enabled.

#### Scenario: Trace emitted for picture-description call

- **GIVEN** a conversion worker is configured with MLflow tracing enabled
- **WHEN** a conversion job performs a picture-description upstream chat completion call
- **THEN** the system records a trace span with operation name, model identifier, provider endpoint type, start/end timing, and success status

#### Scenario: Trace emitted for batch model calls

- **GIVEN** MLflow tracing is enabled and the shared batch utility is in use
- **WHEN** batch chat or batch embeddings processing is executed through the shared batch utility
- **THEN** the system records trace spans that identify batch operation type, model identifier, request count, duration, and final status

### Requirement: Span naming and type separation

The system SHALL use stable action-oriented span names and explicit MLflow span types to separate embedding and LLM traces for queryability.

#### Scenario: Embedding span classification

- **GIVEN** MLflow tracing is enabled
- **WHEN** an embedding operation is traced
- **THEN** the span uses an embedding span type and an embedding-specific operation name

#### Scenario: LLM span classification

- **GIVEN** MLflow tracing is enabled
- **WHEN** a chat completion or equivalent LLM generation operation is traced
- **THEN** the span uses an LLM-oriented span type and an LLM-specific operation name

### Requirement: Embedding trace attributes are minimal

The system MUST record only model, latency, and status as required attributes for embedding spans.

#### Scenario: Embedding attribute payload

- **GIVEN** an embedding operation is executed with MLflow tracing enabled
- **WHEN** an embedding span is emitted
- **THEN** the required attributes include model identifier, duration/latency, and operation status and do not require additional GenAI payload fields

### Requirement: LLM tracing captures GenAI execution context

Every emitted LLM span SHALL carry the model identifier, operation timing, and final operation status; and SHALL additionally carry, whenever the underlying call exposes them, token usage and provider/request metadata sufficient to reconstruct the call for debugging.

#### Scenario: LLM span includes required core attributes

- **GIVEN** a chat completion operation is executed with MLflow tracing enabled
- **WHEN** the LLM call span is emitted
- **THEN** the span carries the model identifier, operation timing, and final operation status

#### Scenario: LLM span carries provider context when available

- **GIVEN** a chat completion call returns token usage and provider/request metadata
- **WHEN** the LLM call span is emitted
- **THEN** the span additionally carries those fields in a form suitable for debugging

### Requirement: Tracing is optional and non-disruptive

The system MUST preserve existing conversion behavior when MLflow tracing is disabled, misconfigured, or temporarily unavailable.

#### Scenario: Tracing disabled

- **GIVEN** MLflow tracing configuration is absent or disabled
- **WHEN** a model call is made
- **THEN** model calls execute normally and no trace emission is attempted

#### Scenario: Tracing backend failure

- **GIVEN** MLflow tracing is enabled but the tracing backend is temporarily unavailable
- **WHEN** MLflow trace emission raises an exception during a model call
- **THEN** the model call outcome is preserved and the tracing failure is handled without crashing the conversion flow

### Requirement: Trace metadata is sanitized

The system MUST avoid emitting secrets and raw sensitive payload content in MLflow trace attributes.

#### Scenario: API credential protection

- **GIVEN** a model call is made using an API key or authorization header
- **WHEN** tracing metadata is generated for the upstream model call
- **THEN** API keys and authorization headers are never included in trace attributes

#### Scenario: Payload minimization

- **GIVEN** text or document content is sent to an upstream model endpoint
- **WHEN** the trace span is recorded
- **THEN** traces include only safe summary fields (for example counts, sizes, or identifiers) rather than full raw content

## Technical Notes

- **Implementation**: `aizk/conversion/` worker and shared batch utilities
- **Dependencies**: MLflow tracing SDK; upstream LLM and embedding provider clients
