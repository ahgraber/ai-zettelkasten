# Design: Conversion Output Content Endpoints

**Date:** 2026-03-23 **Status:** Approved

## Context

The conversion API currently exposes `GET /v1/bookmarks/{aizk_uuid}/outputs` returning `OutputResponse` records — DB-level metadata (S3 key paths, hashes, docling version, figure count).
The actual conversion artifacts (markdown text, manifest JSON, figure images) live in S3 and are not accessible via the API.

Primary consumer is a downstream Python service (yet to be designed).
A debug UI page is a secondary use case.
The service is small/self-hosted with a limited user base, so proxying S3 content through the API server is acceptable.

Raw S3 passthrough is preferred for the manifest: it was serialized through `ConversionManifest` at write time and is already validated JSON, so re-parsing through Pydantic on read adds no safety and creates a coupling point if the manifest schema evolves.

## Decisions

### Decision: Output-centric routing

**Chosen:** New router at `/v1/outputs/{output_id}/...`

**Rationale:** `output_id` is globally unique and sufficient to locate all artifacts.
Nesting under `/v1/bookmarks/{aizk_uuid}/outputs/{output_id}/...` would add path length with no semantic value.

### Decision: Raw passthrough for all content

**Chosen:** Fetch bytes from S3 and return as `Response` with appropriate `Content-Type`.

**Rationale:** Manifest is already validated JSON.
Markdown is plain text.
Figures are binary images.
Re-parsing any of these through intermediate models adds coupling and latency without benefit at this scale.

### Decision: Presigned URLs deferred

**Chosen:** Direct proxy, not presigned URL redirects.

**Rationale:** Presigned URLs add configuration complexity (TTL, credential policy) for marginal benefit at self-hosted scale.
Revisit if memory pressure becomes a concern.

### Decision: Figure key construction from DB fields

**Chosen:** Construct figure S3 key as `{s3_prefix}/figures/{filename}` using `ConversionOutput.s3_prefix`.

**Rationale:** Figure URIs are only stored in `manifest.json`, not in the DB.
Fetching the manifest on every figure request to extract the URI would add a serial S3 round-trip.
The worker already uses the deterministic pattern `{s3_prefix}/figures/{fig_path.name}`, so constructing the key from `s3_prefix` and the requested `filename` is safe — provided `filename` is validated (see below).

### Decision: Replace `get_s3_client` in `dependencies.py` to return `S3Client`

**Chosen:** Replace the existing `get_s3_client` in `dependencies.py` (which currently returns a raw boto3 client) with one that instantiates and returns an `S3Client` instance.

**Rationale:** `S3Client` already wraps boto3 internally.
The existing `get_s3_client` returns a raw boto3 client that is not used by any current route.
Replacing it gives routes access to `get_object_bytes` via a consistent FastAPI `Depends` pattern.

### Decision: Validate `{filename}` to prevent path traversal

**Chosen:** Reject any `filename` that is empty or contains `/` with a 400 response before constructing the S3 key.

**Rationale:** A `filename` containing `../` or `/` components could escape the `figures/` prefix and serve arbitrary S3 objects.
The worker generates figure filenames via `Path.name`, which always produces a bare filename with no path separators, so this validation matches the expected input domain exactly.

## Architecture

```text
Client
  │
  ▼
GET /v1/outputs/{id}/manifest
GET /v1/outputs/{id}/markdown          ─── outputs router (routes/outputs.py)
GET /v1/outputs/{id}/figures/{name}
  │
  ├── [figures only] validate filename: reject if empty or contains "/" → 400
  │
  ├── DB lookup: ConversionOutput by id (get_db_session)
  │     404 if not found
  │
  └── S3 fetch: s3_client.get_object_bytes(key) (get_s3_client → S3Client)
        S3NotFoundError  → 404
        S3Error          → 502
        success          → Response(content=bytes, media_type=...)
```

## Endpoints

### `GET /v1/outputs/{output_id}/manifest`

- Fetches `ConversionOutput.manifest_key` from S3
- Returns raw bytes, `Content-Type: application/json`

### `GET /v1/outputs/{output_id}/markdown`

- Fetches `ConversionOutput.markdown_key` from S3
- Returns raw bytes, `Content-Type: text/markdown; charset=utf-8`

### `GET /v1/outputs/{output_id}/figures/{filename}`

- Rejects `filename` that is empty or contains `/` → 400
- Constructs key: `{ConversionOutput.s3_prefix}/figures/{filename}`
- Infers `Content-Type` from file extension: `png → image/png`, `jpg/jpeg → image/jpeg`, `gif → image/gif`, `webp → image/webp`; falls back to `application/octet-stream`
- Returns raw bytes
- Note: if the output has no figures (`figure_count == 0`), any request here will resolve to a missing S3 key and return 404

## S3Client Changes

Add to `src/aizk/conversion/storage/s3_client.py`:

```python
class S3NotFoundError(S3Error):
    """Raised when a requested S3 object does not exist."""

    retryable: ClassVar[bool] = False

    def __init__(self, key: str):
        super().__init__(f"S3 object not found: {key}", "s3_not_found")


def get_object_bytes(self, s3_key: str) -> bytes:
    """Fetch object bytes from S3.

    Raises:
        S3NotFoundError: If the key does not exist (non-retryable).
        S3Error: For other S3 failures (retryable).
    """
```

## Error Handling

| Condition                            | HTTP status           |
| ------------------------------------ | --------------------- |
| `output_id` not an integer           | 422 (FastAPI default) |
| `output_id` not in DB                | 404                   |
| `filename` empty or contains `/`     | 400                   |
| S3 key not found (`S3NotFoundError`) | 404                   |
| Other S3 error (`S3Error`)           | 502                   |

## Files Changed

| File                                                  | Change                                        |
| ----------------------------------------------------- | --------------------------------------------- |
| `src/aizk/conversion/storage/s3_client.py`            | Add `S3NotFoundError`; add `get_object_bytes` |
| `src/aizk/conversion/api/dependencies.py`             | Replace `get_s3_client` to return `S3Client`  |
| `src/aizk/conversion/api/routes/outputs.py`           | New router with three endpoints               |
| `src/aizk/conversion/api/routes/__init__.py`          | Export `outputs_router`                       |
| `src/aizk/conversion/api/main.py`                     | Register `outputs_router`                     |
| `tests/conversion/unit/test_s3_client.py`             | Tests for `get_object_bytes`                  |
| `tests/conversion/integration/test_output_content.py` | Integration tests for all three endpoints     |

## Testing

**Unit (`test_s3_client.py`):** mock `boto3` client; assert success returns bytes; assert `S3NotFoundError` on 404 `ClientError`; assert `S3Error` on other `ClientError`.

**Integration (`test_output_content.py`):** monkeypatch `S3Client.get_object_bytes`; assert correct bytes and `Content-Type` headers for each endpoint; assert 404 on unknown `output_id`; assert 404 when `get_object_bytes` raises `S3NotFoundError`; assert 502 when `get_object_bytes` raises `S3Error`; assert 400 on `filename` containing `/` or `..`; assert correct S3 key construction for figures; assert 404 for output with `figure_count == 0`.
