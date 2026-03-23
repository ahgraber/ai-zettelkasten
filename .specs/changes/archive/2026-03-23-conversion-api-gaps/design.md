# Design: Conversion Output Content Endpoints

## Context

The conversion API exposes `GET /v1/bookmarks/{aizk_uuid}/outputs` returning DB-level metadata.
Actual artifacts (markdown, manifest JSON, figures) live in S3.
Primary consumer is a downstream Python service; a debug UI is a secondary use case.
The service is small/self-hosted, so proxying S3 content through the API is acceptable.
Memory pressure is explicitly deferred.

The manifest was serialized through `ConversionManifest` at write time and is already validated JSON.
Re-parsing it on read adds no safety and couples the API to the manifest schema.

## Decisions

### Decision: Output-centric routing

**Chosen:** New router at `/v1/outputs/{output_id}/...`

**Rationale:** `output_id` is globally unique.
Nesting under `/v1/bookmarks/...` adds path length with no semantic value.

**Alternatives considered:**

- Nested under bookmarks: adds no value, `output_id` is sufficient.

### Decision: Raw passthrough for all content

**Chosen:** Fetch bytes from S3, return as `Response` with appropriate `Content-Type`.

**Rationale:** Manifest is already validated JSON.
Re-parsing through Pydantic adds coupling.
Markdown and figures are opaque bytes.

**Alternatives considered:**

- Parse manifest through `ConversionManifest` on read: couples API to manifest schema evolution, no benefit.

### Decision: Figure key construction from `s3_prefix`

**Chosen:** Construct figure S3 key as `{s3_prefix}/figures/{filename}`.

**Rationale:** Figure URIs are stored only in `manifest.json`, not in the DB.
Fetching the manifest on every figure request adds a serial S3 round-trip.
The worker uses the deterministic pattern `{s3_prefix}/figures/{fig_path.name}`, so key construction from `s3_prefix` is safe — provided `filename` is validated.

**Alternatives considered:**

- Fetch manifest to extract figure URI: extra S3 round-trip, unnecessary.

### Decision: Replace `get_s3_client` in `dependencies.py`

**Chosen:** Replace the existing `get_s3_client` in `dependencies.py` (currently returns an unused raw boto3 client) with one that returns an `S3Client` instance.

**Rationale:** `S3Client` wraps boto3 internally.
Routes need `get_object_bytes`, which lives on `S3Client`, not on a raw boto3 client.

### Decision: Validate `{filename}` before S3 key construction

**Chosen:** Reject filenames that are empty or contain `/` with a 4xx error response.

**Rationale:** Worker generates figure filenames via `Path.name` (always a bare name, no separators).
A `/` in `filename` would escape the `figures/` prefix and allow serving arbitrary S3 objects.
Filenames containing `/` are rejected at the router level (404) before the handler runs; empty filenames are caught by handler validation (400).
Either way, no S3 access occurs.

## Architecture

```text
Client
  │
  ▼
GET /v1/outputs/{id}/manifest
GET /v1/outputs/{id}/markdown          ─── outputs router (routes/outputs.py)
GET /v1/outputs/{id}/figures/{name}
  │
  ├── [figures only] validate filename: reject empty or contains "/" → 400
  │
  ├── DB lookup: ConversionOutput by id  (get_db_session)
  │     404 if not found
  │
  └── S3 fetch: s3_client.get_object_bytes(key)  (get_s3_client → S3Client)
        S3NotFoundError  → 404
        S3Error          → 502
        success          → Response(content=bytes, media_type=...)
```

## Risks

- **Memory pressure from large markdown files**: accepted at self-hosted scale; revisit with streaming if needed.
- **Figure key construction diverges from worker**: if the worker changes its figure naming convention, the API will silently return 404.
  The deterministic convention should be documented as a cross-component contract.
