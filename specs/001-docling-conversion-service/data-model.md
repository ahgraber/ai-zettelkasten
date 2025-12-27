# Data Model: Docling Conversion Service

**Feature**: 001-docling-conversion-service
**Date**: 2025-12-23
**Purpose**: Define database schema, entities, relationships, and validation rules

## Overview

This document defines the complete data model for the Docling Conversion Service, including SQLite schema with SQLModel models, relationships, indexes, and validation rules.

## Database Technology

- **Engine**: SQLite 3.35+ (for WAL mode and JSON support)
- **ORM**: SQLModel (Pydantic + SQLAlchemy)
- **Connection Settings**:
  - journal_mode=WAL (Write-Ahead Logging for concurrent reads)
  - synchronous=NORMAL (balance durability and performance)
  - busy_timeout=5000 (milliseconds)
  - foreign_keys=ON

## Entities

### Bookmark

**Purpose**: Represents a KaraKeep bookmark with metadata needed for conversion execution.

**Storage**: Table `bookmarks`

**Fields**:

| Field          | Type        | Constraints                         | Description                                                                      |
| -------------- | ----------- | ----------------------------------- | -------------------------------------------------------------------------------- |
| id             | Integer     | PRIMARY KEY, AUTOINCREMENT          | Internal database ID                                                             |
| karakeep_id    | String(255) | UNIQUE, NOT NULL, INDEXED           | External KaraKeep bookmark identifier                                            |
| aizk_uuid      | String(36)  | UNIQUE, NOT NULL, INDEXED           | Internal UUID for this bookmark (UUID4)                                          |
| url            | Text        | NOT NULL                            | Original source URL as submitted                                                 |
| normalized_url | Text        | NOT NULL, INDEXED                   | Normalized URL for deduplication                                                 |
| title          | Text        | NOT NULL                            | Bookmark title from KaraKeep or extracted                                        |
| content_type   | String(10)  | NOT NULL                            | Format of content: 'html' or 'pdf' (from KaraKeep metadata or detected from URL) |
| source_type    | String(20)  | NOT NULL                            | Origin/source of URL: 'arxiv', 'github', or 'other' (parsed from URL pattern)    |
| created_at     | DateTime    | NOT NULL, DEFAULT CURRENT_TIMESTAMP | Record creation timestamp (UTC)                                                  |
| updated_at     | DateTime    | NOT NULL, DEFAULT CURRENT_TIMESTAMP | Record update timestamp (UTC)                                                    |

**Relationships**:

- One-to-many with ConversionJob (via aizk_uuid)
- One-to-many with ConversionOutput (via aizk_uuid)

**Validation Rules**:

- `karakeep_id`: Non-empty string, max 255 chars
- `aizk_uuid`: Valid UUID4 format
- `url`: Valid URL format (validated by pydantic HttpUrl)
- `normalized_url`: Computed from url via normalization function
- `content_type`: Must be one of: 'html', 'pdf' (from KaraKeep metadata)
- `source_type`: Must be one of: 'arxiv', 'github', 'other' (parsed from URL domain/pattern)
- `title`: Non-empty string, max 500 chars (truncate with ellipsis if needed)

**Business Rules**:

- `karakeep_id` is unique across all bookmarks
- `content_type` is typically provided by KaraKeep bookmark metadata; if missing, detect from URL extension (.pdf → 'pdf', otherwise 'html')
- `source_type` is always parsed from URL pattern regardless of content_type:
  - Contains 'arxiv.org' → 'arxiv'
  - Contains 'github.com' → 'github'
  - Otherwise → 'other'

**Indexes**:

- SQLModel defaults with `Field(index=True)` on `karakeep_id`, `aizk_uuid`, `normalized_url`

---

### ConversionJob

**Purpose**: Represents a single conversion attempt with status tracking and retry logic.

**Storage**: Table `conversion_jobs`

**Fields**:

| Field           | Type       | Constraints                                          | Description                                                                                                     |
| --------------- | ---------- | ---------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| id              | Integer    | PRIMARY KEY, AUTOINCREMENT                           | Internal job ID                                                                                                 |
| aizk_uuid       | String(36) | FOREIGN KEY → bookmarks.aizk_uuid, NOT NULL, INDEXED | Reference to bookmark                                                                                           |
| payload_version | Integer    | NOT NULL, DEFAULT 1                                  | API/pipeline version for reprocessing                                                                           |
| status          | String(20) | NOT NULL, INDEXED                                    | Enum: 'NEW', 'QUEUED', 'RUNNING', 'UPLOAD_PENDING', 'SUCCEEDED', 'FAILED_RETRYABLE', 'FAILED_PERM', 'CANCELLED' |
| attempts        | Integer    | NOT NULL, DEFAULT 0                                  | Number of execution attempts                                                                                    |
| error_code      | String(50) | NULLABLE                                             | Machine-readable error identifier                                                                               |
| error_message   | Text       | NULLABLE                                             | Human-readable error details                                                                                    |
| idempotency_key | String(64) | UNIQUE, NOT NULL, INDEXED                            | Hash for duplicate detection                                                                                    |
| next_attempt_at | DateTime   | NULLABLE, INDEXED                                    | Scheduled retry timestamp (UTC)                                                                                 |
| last_error_at   | DateTime   | NULLABLE                                             | Most recent error timestamp (UTC)                                                                               |
| queued_at       | DateTime   | NULLABLE                                             | When job entered QUEUED status                                                                                  |
| started_at      | DateTime   | NULLABLE                                             | When job entered RUNNING status                                                                                 |
| finished_at     | DateTime   | NULLABLE                                             | When job reached terminal status                                                                                |
| created_at      | DateTime   | NOT NULL, DEFAULT CURRENT_TIMESTAMP                  | Record creation timestamp (UTC)                                                                                 |
| updated_at      | DateTime   | NOT NULL, DEFAULT CURRENT_TIMESTAMP                  | Record update timestamp (UTC)                                                                                   |

**Relationships**:

- Many-to-one with Bookmark (via aizk_uuid)
- One-to-one with ConversionOutput (via job_id, only if SUCCEEDED)

**Validation Rules**:

- `aizk_uuid`: Valid UUID4 format, must exist in bookmarks table
- `payload_version`: Positive integer
- `status`: Must be one of allowed enum values
- `attempts`: Non-negative integer, max 10 (configurable)
- `error_code`: If present, must be from defined error code list
- `idempotency_key`: 64-character hex string (SHA256 digest)

**Business Rules**:

- `idempotency_key` = hash(aizk_uuid + payload_version + docling_version + config_hash). Implementation uses SHA256 for deterministic hex output (see research.md ADR-002).
- Status transitions:
  - NEW → QUEUED (on submission)
  - QUEUED → RUNNING (worker picks up job)
  - RUNNING → UPLOAD_PENDING (conversion successful, ready for S3 upload)
  - UPLOAD_PENDING → SUCCEEDED (S3 artifacts verified uploaded)
  - RUNNING → FAILED_RETRYABLE (fetch or conversion error, can retry full flow)
  - UPLOAD_PENDING → FAILED_RETRYABLE (S3 upload error, can retry from upload step without reconverting)
  - RUNNING/UPLOAD_PENDING → FAILED_PERM (permanent error or max attempts reached)
  - QUEUED/RUNNING/UPLOAD_PENDING → CANCELLED (user cancellation)
  - FAILED_RETRYABLE → QUEUED (manual or automatic retry)
- `next_attempt_at` computed with exponential backoff: base_delay * (2 \*\* attempts)
- `queued_at` set when status → QUEUED
- `started_at` set when status → RUNNING
- `finished_at` set when status → SUCCEEDED/FAILED_PERM/CANCELLED
- `attempts` incremented on each RUNNING transition

**Error Codes** (examples):

- `fetch_timeout`: Source URL fetch exceeded timeout
- `fetch_404`: Source URL returned 404
- `fetch_size_exceeded`: Source content exceeded size limit
- `docling_error`: Docling conversion raised exception
- `docling_empty_output`: Docling produced no Markdown content
- `s3_upload_failed`: S3 upload operation failed
- `arxiv_unavailable`: arXiv HTML and PDF both unavailable
- `github_readme_not_found`: GitHub repository has no README

**Indexes**:

- SQLModel defaults with `Field(index=True)` on `aizk_uuid`, `status`, `idempotency_key`, `next_attempt_at`, `created_at`

---

### ConversionOutput

**Purpose**: Represents successful conversion artifact set with S3 locations and content metadata.

**Storage**: Table `conversion_outputs`

**Fields**:

| Field              | Type       | Constraints                                                 | Description                           |
| ------------------ | ---------- | ----------------------------------------------------------- | ------------------------------------- |
| id                 | Integer    | PRIMARY KEY, AUTOINCREMENT                                  | Internal output ID                    |
| job_id             | Integer    | FOREIGN KEY → conversion_jobs.id, NOT NULL, UNIQUE, INDEXED | Reference to completed job            |
| aizk_uuid          | String(36) | FOREIGN KEY → bookmarks.aizk_uuid, NOT NULL, INDEXED        | Reference to bookmark                 |
| payload_version    | Integer    | NOT NULL                                                    | API/pipeline version used             |
| s3_prefix          | Text       | NOT NULL                                                    | S3 path prefix: `bucket/<aizk_uuid>/` |
| markdown_key       | Text       | NOT NULL                                                    | S3 key for Markdown file              |
| manifest_key       | Text       | NOT NULL                                                    | S3 key for manifest.json              |
| markdown_hash_xx64 | String(16) | NOT NULL, INDEXED                                           | xxHash64 of normalized Markdown       |
| figure_count       | Integer    | NOT NULL, DEFAULT 0                                         | Number of extracted figures           |
| docling_version    | String(20) | NOT NULL                                                    | Docling library version used          |
| pipeline_name      | String(50) | NOT NULL                                                    | Pipeline name: 'html' or 'pdf'        |
| created_at         | DateTime   | NOT NULL, DEFAULT CURRENT_TIMESTAMP                         | Record creation timestamp (UTC)       |

**Relationships**:

- Many-to-one with Bookmark (via aizk_uuid)
- One-to-one with ConversionJob (via job_id)

**Validation Rules**:

- `job_id`: Must reference existing job with status=SUCCEEDED
- `aizk_uuid`: Valid UUID4 format, must exist in bookmarks table
- `payload_version`: Positive integer
- `s3_prefix`: Non-empty string, format: `s3://bucket/<aizk_uuid>/`
- `markdown_key`: Non-empty string, format: `<s3_prefix><filename>.md`
- `manifest_key`: Non-empty string, format: `<s3_prefix>manifest.json`
- `markdown_hash_xx64`: 16-character hex string (xxHash64 digest)
- `figure_count`: Non-negative integer
- `docling_version`: Semantic version string (e.g., '2.65.0')
- `pipeline_name`: Must be 'html' or 'pdf'

**Business Rules**:

- `markdown_hash_xx64` computed from normalized Markdown: UTF-8 bytes, LF line endings, trimmed whitespace
- `s3_prefix` constructed as: `s3://{bucket}/{aizk_uuid}/`
- `markdown_key` constructed as: `{s3_prefix}{normalized_title}.md`
- `manifest_key` constructed as: `{s3_prefix}manifest.json`
- New output with same `markdown_hash_xx64` as previous output for same `aizk_uuid` reuses existing S3 artifacts (no overwrite)
- `docling_version` extracted from docling.**version** at runtime

**Indexes**:

- SQLModel defaults with `Field(index=True)` on `job_id`, `aizk_uuid`, `markdown_hash_xx64`, `created_at`

---

## Manifest Schema

**Purpose**: JSON file stored in S3 alongside Markdown and figures, containing complete artifact inventory and metadata.

**S3 Location**: `s3://{bucket}/{aizk_uuid}/manifest.json`

**Schema**:

```json
{
  "version": "1.0",
  "aizk_uuid": "550e8400-e29b-41d4-a716-446655440000",
  "karakeep_id": "bm_abc123",
  "source": {
    "url": "https://arxiv.org/abs/1706.03762",
    "normalized_url": "https://arxiv.org/abs/1706.03762",
    "title": "Attention Is All You Need",
    "source_type": "arxiv",
    "fetched_at": "2025-12-23T10:30:00Z"
  },
  "conversion": {
    "job_id": 42,
    "payload_version": 1,
    "docling_version": "2.65.0",
    "pipeline_name": "html",
    "started_at": "2025-12-23T10:30:05Z",
    "finished_at": "2025-12-23T10:31:20Z",
    "duration_seconds": 75
  },
  "artifacts": {
    "markdown": {
      "key": "s3://bucket/550e8400-e29b-41d4-a716-446655440000/attention-is-all-you-need.md",
      "hash_xx64": "1234567890abcdef",
      "created_at": "2025-12-23T10:31:15Z"
    },
    "figures": [
      {
        "key": "s3://bucket/550e8400-e29b-41d4-a716-446655440000/figure1.png",
        "description": "Transformer model architecture",
        "created_at": "2025-12-23T10:31:16Z"
      },
      {
        "key": "s3://bucket/550e8400-e29b-41d4-a716-446655440000/figure2.png",
        "description": "Attention visualization",
        "created_at": "2025-12-23T10:31:17Z"
      }
    ]
  }
}
```

**Validation**:

- All timestamps in ISO 8601 UTC format
- All S3 keys use absolute URIs: `s3://bucket/path`
- Figure count in manifest must match `conversion_outputs.figure_count`
- Markdown hash must match `conversion_outputs.markdown_hash_xx64`

---

## Entity Relationships Diagram

```text
┌─────────────────────┐
│    Bookmark         │
├─────────────────────┤
│ id (PK)             │
│ karakeep_id (UK)    │
│ aizk_uuid (UK)      │──┐
│ url                 │  │
│ normalized_url      │  │
│ title               │  │
│ source_type         │  │
│ created_at          │  │
│ updated_at          │  │
└─────────────────────┘  │
                          │
                          │ 1:N
                          │
                          ▼
┌─────────────────────┐  │
│  ConversionJob      │  │
├─────────────────────┤  │
│ id (PK)             │  │
│ aizk_uuid (FK) ─────┘  │
│ payload_version     │  │
│ status              │  │
│ attempts            │  │
│ error_code          │  │
│ error_message       │  │
│ idempotency_key(UK) │  │
│ next_attempt_at     │  │
│ queued_at           │  │
│ started_at          │  │
│ finished_at         │  │
│ created_at          │  │
│ updated_at          │  │
└─────────────────────┘  │
           │              │
           │ 1:1          │
           │              │
           ▼              │
┌─────────────────────┐  │
│ ConversionOutput    │  │
├─────────────────────┤  │
│ id (PK)             │  │
│ job_id (FK,UK)      │  │
│ aizk_uuid (FK) ─────┘
│ payload_version     │
│ s3_prefix           │
│ markdown_key        │
│ manifest_key        │
│ markdown_hash_xx64  │
│ figure_count        │
│ docling_version     │
│ pipeline_name       │
│ created_at          │
└─────────────────────┘
```

## URL Normalization Algorithm

**Purpose**: Consistent URL comparison for deduplication across bookmarks.

**Implementation**:

```python
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse


def normalize_url(url: str) -> str:
    """Normalize URL for deduplication.

    Rules:
    - Lowercase scheme and domain
    - Remove fragment (#anchor)
    - Sort query parameters
    - Remove default ports (80, 443)
    - Remove trailing slash on path (unless root)
    """
    parsed = urlparse(url)

    # Lowercase scheme and domain
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()

    # Remove default ports
    if netloc.endswith(":80") and scheme == "http":
        netloc = netloc[:-3]
    if netloc.endswith(":443") and scheme == "https":
        netloc = netloc[:-4]

    # Sort query parameters
    query_params = parse_qs(parsed.query, keep_blank_values=True)
    sorted_query = urlencode(sorted(query_params.items()), doseq=True)

    # Remove trailing slash (unless root path)
    path = parsed.path.rstrip("/") if parsed.path != "/" else parsed.path

    # Reconstruct without fragment
    normalized = urlunparse((scheme, netloc, path, parsed.params, sorted_query, ""))

    return normalized
```

**Examples**:

- `https://Example.COM/Path?b=2&a=1#anchor` → `https://example.com/Path?a=1&b=2`
- `http://example.com:80/` → `http://example.com/`
- `https://example.com/path/` → `https://example.com/path`

---

## Markdown Hash Algorithm

**Purpose**: Content-based deduplication to detect when reprocessing produces identical output.

**Implementation**:

```python
import xxhash


def compute_markdown_hash(markdown: str) -> str:
    """Compute xxHash64 of normalized Markdown.

    Normalization:
    - Encode as UTF-8
    - Convert line endings to LF
    - Strip leading/trailing whitespace
    """
    normalized = markdown.strip().replace("\r\n", "\n").replace("\r", "\n")
    digest = xxhash.xxh64(normalized.encode("utf-8")).hexdigest()
    return digest
```

**Why xxHash64**:

- Fast (5-10GB/s throughput)
- Good distribution for hash table use
- 64-bit digest (16 hex chars) sufficient for deduplication
- Non-cryptographic (acceptable since not used for security)

---

## Idempotency Key Algorithm

**Purpose**: Prevent duplicate processing of identical conversion requests.

**Implementation**:

```python
import hashlib


def compute_idempotency_key(aizk_uuid: str, payload_version: int, docling_version: str, config_hash: str) -> str:
    """Compute SHA256 idempotency key.

    Args:
        aizk_uuid: Bookmark UUID
        payload_version: API/pipeline version
        docling_version: Docling library version
        config_hash: Hash of conversion config (pipeline options)

    Returns:
        64-character hex string (SHA256 digest)
    """
    components = f"{aizk_uuid}:{payload_version}:{docling_version}:{config_hash}"
    digest = hashlib.sha256(components.encode("utf-8")).hexdigest()
    return digest
```

**Config Hash Computation**:

```python
def compute_config_hash(pipeline_options: dict) -> str:
    """Hash of serialized pipeline configuration."""
    import json

    config_json = json.dumps(pipeline_options, sort_keys=True)
    return hashlib.sha256(config_json.encode("utf-8")).hexdigest()[:16]
```

---

## SQLModel Examples

**Bookmark Model**:

```python
from datetime import datetime
from typing import Optional
from sqlmodel import Field, SQLModel, Relationship
from uuid import UUID, uuid4


class Bookmark(SQLModel, table=True):
    __tablename__ = "bookmarks"

    id: Optional[int] = Field(default=None, primary_key=True)
    karakeep_id: str = Field(max_length=255, unique=True, index=True)
    aizk_uuid: str = Field(default_factory=lambda: str(uuid4()), unique=True, index=True)
    url: str
    normalized_url: str = Field(index=True)
    title: str = Field(max_length=500)
    source_type: str = Field(max_length=20)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    # Relationships
    jobs: list["ConversionJob"] = Relationship(back_populates="bookmark")
    outputs: list["ConversionOutput"] = Relationship(back_populates="bookmark")
```

**ConversionJob Model**:

```python
from datetime import datetime
from typing import Optional
from sqlmodel import Field, SQLModel, Relationship


class ConversionJob(SQLModel, table=True):
    __tablename__ = "conversion_jobs"

    id: Optional[int] = Field(default=None, primary_key=True)
    aizk_uuid: str = Field(foreign_key="bookmarks.aizk_uuid", index=True)
    payload_version: int = Field(default=1)
    status: str = Field(max_length=20, index=True)
    attempts: int = Field(default=0)
    error_code: Optional[str] = Field(default=None, max_length=50)
    error_message: Optional[str] = Field(default=None)
    idempotency_key: str = Field(max_length=64, unique=True, index=True)
    next_attempt_at: Optional[datetime] = Field(default=None, index=True)
    last_error_at: Optional[datetime] = Field(default=None)
    queued_at: Optional[datetime] = Field(default=None)
    started_at: Optional[datetime] = Field(default=None)
    finished_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    # Relationships
    bookmark: Bookmark = Relationship(back_populates="jobs")
    output: Optional["ConversionOutput"] = Relationship(back_populates="job")
```

**ConversionOutput Model**:

```python
from datetime import datetime
from typing import Optional
from sqlmodel import Field, SQLModel, Relationship


class ConversionOutput(SQLModel, table=True):
    __tablename__ = "conversion_outputs"

    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="conversion_jobs.id", unique=True, index=True)
    aizk_uuid: str = Field(foreign_key="bookmarks.aizk_uuid", index=True)
    payload_version: int
    s3_prefix: str
    markdown_key: str
    manifest_key: str
    markdown_hash_xx64: str = Field(max_length=16, index=True)
    figure_count: int = Field(default=0)
    docling_version: str = Field(max_length=20)
    pipeline_name: str = Field(max_length=50)
    created_at: datetime = Field(default_factory=datetime.utcnow)

    # Relationships
    bookmark: Bookmark = Relationship(back_populates="outputs")
    job: ConversionJob = Relationship(back_populates="output")
```

---

## Migration Strategy

**Initial Schema Creation**: Use SQLModel's `SQLModel.metadata.create_all(engine)` for initial deployment.

**Future Schema Changes**: Use Alembic for migrations when schema evolves:

1. Create migration: `alembic revision --autogenerate -m "description"`
2. Review generated migration for correctness
3. Apply: `alembic upgrade head`
4. Document in CHANGELOG.md with MINOR version bump

**Backward Compatibility**: When adding nullable fields or new tables, ensure old code can read new schema.

---

## Summary

This data model provides:

✅ **Complete entity definitions** for Bookmark, ConversionJob, ConversionOutput\
✅ **Clear relationships** between entities with foreign keys\
✅ **Proper indexes** for query performance (status lookups, UUID lookups, time-range queries)\
✅ **Validation rules** for all fields with business constraints\
✅ **Algorithms** for URL normalization, Markdown hashing, idempotency keys\
✅ **Manifest schema** for S3 artifact inventory\
✅ **SQLModel examples** ready for implementation\
✅ **Migration strategy** for schema evolution

Ready to proceed to API contract definition in Phase 1.
