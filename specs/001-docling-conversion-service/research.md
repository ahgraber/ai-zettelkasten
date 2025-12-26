# Research: Docling Conversion Service

**Feature**: 001-docling-conversion-service
**Date**: 2025-12-23
**Updated**: 2025-12-26 (spec compliance review)
**Purpose**: Resolve technical unknowns and document design decisions for implementation planning

## Overview

This document resolves NEEDS CLARIFICATION items identified in Technical Context and documents architectural decisions required by the constitution.

**Spec Update Note**: The following ADR decisions have been elevated to spec-level implementation directives in the "Technical Context & Implementation Directives" section:

- S3 Storage Strategy: Atomic uploads with verification checksums
- Idempotency & Reprocessing: Idempotency_key computation and payload_version semantics

These directives are now normative requirements, not optional architecture recommendations. See spec.md for details.

## Research Items

### R1: Job History Retention and Archive Strategy

**Question**: How long should completed conversion jobs be retained in the database? What is the archive/purge strategy?

**Decision**: Retain all job records indefinitely in SQLite with optional manual purge via API endpoint

**Rationale**:

- SQLite with proper indexing can handle millions of records efficiently
- Conversion history provides valuable debugging and analytics data
- Storage cost is negligible (job metadata is \<1KB per record)
- S3 artifacts are the primary storage concern, not database records
- Operator can implement custom retention policies via API if needed

**Alternatives considered**:

- Auto-purge after 90 days: Rejected because historical data is valuable for analytics and debugging; manual purge is safer
- Move to archive database: Rejected as premature optimization; SQLite performance sufficient for expected scale
- Delete failed jobs after retry exhaustion: Rejected because failure patterns provide important operational insights

**Implementation notes**:

- Add optional GET /v1/jobs?created_before={timestamp} filter for querying old jobs
- Add optional DELETE /v1/jobs/purge?created_before={timestamp}&status={SUCCEEDED|FAILED_PERM} for manual cleanup
- Document recommended purge intervals in operational runbook

---

### R2: S3 Bucket Quota and Cost Management

**Question**: What is the expected S3 bucket quota? How should we handle quota exhaustion?

**Decision**: Assume unlimited S3 capacity for initial deployment; add monitoring for bucket size with alerting at configurable threshold

**Rationale**:

- S3 cost is ~$0.023/GB/month (standard tier); hundreds of bookmarks per day = ~1-5GB/month with average bookmark size
- Bucket quotas are rarely an issue with S3 (soft limits at 100TB+)
- More important to monitor growth rate and set cost alerts
- Quota exhaustion should fail jobs gracefully with FAILED_RETRYABLE status

**Alternatives considered**:

- Implement hard quota enforcement: Rejected as premature; monitoring + alerting sufficient initially
- Compress Markdown and images: Rejected because slight storage savings not worth complexity and retrieval latency
- Delete old artifacts after N days: Rejected because artifacts are primary value; let operator decide retention policy

**Implementation notes**:

- S3 upload failures due to quota/permissions mark job as FAILED_RETRYABLE with specific error_code
- Add optional S3 bucket size metric emission (computed periodically or on-demand)
- Document cost estimation in operational runbook: ~$0.01-0.05/bookmark/month assuming 1-5MB average size

---

### R3: SQLite vs PostgreSQL for Production Deployment

**Question**: SQLite is constitution default, but should we support PostgreSQL for production deployments with higher scale?

**Decision**: Start with SQLite only; design schema/queries to be SQLModel-compatible for future Postgres migration if needed

**Rationale**:

- Constitution specifies SQLite as default; deviations require ADR
- SQLite WAL mode supports 4 concurrent workers (spec requirement) without contention
- Expected scale (hundreds of bookmarks/day, 4 workers) well within SQLite capabilities
- SQLModel abstraction allows DB backend swap with minimal code changes
- Premature to add Postgres complexity without production evidence of SQLite limitations

**Alternatives considered**:

- Start with Postgres: Rejected due to constitution default and added deployment complexity (separate service, connection pooling, migrations)
- Support both via configuration: Rejected as YAGNI; add when real production need emerges
- Use DuckDB instead: Rejected because SQLModel integration immature and conversion workload doesn't benefit from columnar format

**Implementation notes**:

- Use SQLModel for all database access (not raw SQLAlchemy)
- Avoid SQLite-specific SQL features (use portable SQLModel constructs)
- Document SQLite→Postgres migration path in runbook
- Add SQLite connection settings: WAL mode, synchronous=NORMAL, busy_timeout=5000ms
- This decision will be documented in ADR-003

---

## Architectural Decision Records (ADRs)

The following ADRs must be created in `docs/decision-record/` directory:

### ADR-001: S3 Storage Strategy and Atomic Finalization

**Context**: Conversion service must upload multiple artifacts (Markdown, figures, manifest) to S3 and ensure consumers never see incomplete uploads.

**Decision**:

1. Upload all artifacts to S3 first with individual PutObject operations
2. Verify all uploads succeeded (check ETag or HTTP 200)
3. Update conversion_jobs.status=SUCCEEDED in database transaction only after all uploads confirmed
4. Use conversion_outputs.s3_prefix as "commit marker" - consumers only query conversion_outputs, never see jobs without completed outputs

**Consequences**:

- Positive: Strong atomicity guarantee - consumers never fetch partial artifacts
- Positive: Simple implementation - no S3-specific features required
- Negative: Failed uploads leave orphaned objects in S3 (mitigated by periodic cleanup scan)
- Negative: Non-transactional across S3 and database (acceptable given internal-only deployment)

**Alternatives rejected**:

- S3 batch operations: Not needed for small artifact counts (typically \<50 files)
- Two-phase commit across S3 and database: Overkill for internal service; database as source of truth is sufficient

---

### ADR-002: Idempotency and Payload Version Semantics

**Context**: Service must prevent duplicate processing of same bookmark while allowing reprocessing after Docling upgrades or configuration changes.

**Decision**:

1. Compute idempotency_key = hash(aizk_uuid + payload_version + docling_version + config_hash)
2. Enforce unique constraint on conversion_jobs.idempotency_key
3. Payload version increments manually or via CI/CD when conversion logic changes
4. Reprocessing with new payload_version creates new job despite existing output
5. Compare markdown_hash_xx64 to detect if reprocessed output differs from previous version

**Consequences**:

- Positive: Clear semantics - duplicate submissions rejected, intentional reprocessing allowed
- Positive: Content-based deduplication via markdown_hash_xx64 prevents overwriting identical outputs
- Negative: Requires manual payload_version management (mitigated by clear documentation and CI automation)

**Alternatives rejected**:

- URL-only deduplication: Insufficient because same URL may need reprocessing after Docling upgrades
- Automatic change detection: Complex and unreliable; explicit versioning clearer for operators

---

### ADR-003: SQLite with WAL Mode for Initial Deployment

**Context**: Service needs persistent job queue and metadata storage with 4 concurrent workers.

**Decision**: Use SQLite in WAL (Write-Ahead Logging) mode with:

- synchronous=NORMAL (balance durability and performance)
- busy_timeout=5000ms (handle writer contention gracefully)
- Single writer pattern with retry logic in application code
- Prepared statements for all queries
- Proper indexes: idx_jobs_status_next_attempt, idx_bookmarks_normalized_url, idx_outputs_aizk_uuid

**Consequences**:

- Positive: Zero-configuration deployment (no separate database service)
- Positive: ACID transactions with crash recovery via WAL
- Positive: Sufficient for expected scale (4 workers, hundreds of jobs/day)
- Positive: Simple backup (copy .db file during low activity)
- Negative: Not suitable for high write concurrency (>10 workers) or distributed deployment
- Negative: Requires migration to Postgres if scale exceeds SQLite capabilities

**When to migrate**: Consider Postgres when:

- Concurrent workers exceed 8
- Job submission rate exceeds 1000/hour sustained
- Database size exceeds 10GB
- Multi-region deployment required

**Alternatives rejected**:

- Postgres from start: Violates constitution default, adds deployment complexity without proven need
- Redis for queue + SQLite for metadata: Adds dependency without clear benefit (SQLite handles both)

---

## Best Practices

### Docling HTML and PDF Pipeline Configuration

**Research**: Review docling_demo.py for recommended pipeline configuration patterns

**Findings**:

- Use `HTMLBackendOptions` for HTML sources with appropriate timeout and size limits
- Use `ThreadedPdfPipelineOptions` for PDF sources with page limit configuration
- Enable EasyOCR for PDF with images containing text
- Optional: Enable VLM picture description for figure enrichment (adds latency and API cost)
- Extract figures to separate PNG files using PictureSerializer

**Recommended configuration**:

```python
# HTML pipeline
html_options = HTMLBackendOptions(
    timeout=30,  # 30s per spec
    max_response_size=50 * 1024 * 1024,  # 50MB per spec
)

# PDF pipeline
pdf_options = ThreadedPdfPipelineOptions(
    max_pages=100,  # Configurable page limit per spec edge case
)

# Conversion options
pipeline_options = ConversionPipelineOptions(
    do_ocr=True,  # Enable OCR for PDF images
    do_table_structure=True,  # Extract table structure
    generate_page_images=False,  # Don't need full page images
    generate_picture_images=True,  # Extract figures
)
```

**Docling version pinning**: Use `docling>=2.65,<3.0` in pyproject.toml; record exact resolved version in manifest.json

---

### arXiv Content Fetching Strategy

**Research**: Identify best practices for fetching arXiv content as HTML vs PDF

**Findings**:

- arXiv HTML export at `export.arxiv.org/html/{arxiv_id}` launched 2023, provides semantic HTML with equations as MathML
- HTML export has better structure preservation, faster conversion, and smaller file size than PDF
- Not all papers have HTML export (older papers or author opt-out); fallback to PDF required
- arXiv rate limits: 5 requests/second per IP; implement exponential backoff on 429/503

**Decision**: Prioritize HTML, fallback to PDF

1. Extract arxiv_id from URL using regex: `r'arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})'`
2. Attempt fetch from `https://export.arxiv.org/html/{arxiv_id}`
3. If 404, attempt fetch from `https://export.arxiv.org/pdf/{arxiv_id}`
4. If both fail, mark job FAILED_RETRYABLE with error_code='arxiv_unavailable'

**Implementation notes**:

- Use httpx with timeout=30s and retry logic (3 attempts with exponential backoff)
- Set User-Agent header with project contact info per arXiv API guidelines
- Cache fetched content temporarily in workspace before Docling processing

---

### GitHub README Fetching Strategy

**Research**: Identify best practices for fetching GitHub repository READMEs

**Findings**:

- GitHub raw content API: `https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}`
- Default branch typically `main` or `master`; can query API for default branch if needed
- README naming conventions: README.md, README.rst, README.txt, README
- No authentication required for public repos; rate limit 60 req/hour per IP (sufficient for expected scale)

**Decision**: Simple README fetch without API

1. Extract owner/repo from URL using regex: `r'github\.com/([^/]+)/([^/]+)'`
2. Attempt fetch in order: README.md, README.rst, README.txt, README
3. Use `main` as default branch; if 404 on all, try `master` branch
4. If all attempts fail, mark job FAILED_PERM with error_code='github_readme_not_found'

**Implementation notes**:

- Use httpx with timeout=10s (READMEs are small)
- Handle repository renames/deletions gracefully (404 → FAILED_PERM)
- Don't attempt to fetch from private repos (will 404, acceptable failure)

---

### Markdown Filename Normalization

**Research**: Identify best practices for cross-OS compatible filename generation from bookmark titles

**Findings**:

- Windows: reserved characters `< > : " / \ | ? *`, max path 260 chars
- macOS/Linux: only `/` and null byte reserved, max filename 255 bytes
- Best practice: lowercase, replace special chars with hyphens, strip leading/trailing dots/dashes

**Decision**: Conservative normalization for maximum compatibility

Use existing aizk.utilities.file_utils.to_valid_fname

**Implementation notes**:

- Apply to bookmark title when generating Markdown filename: `{normalized_title}.md`
- Store original title in manifest.json for reference
- Handle collisions via aizk_uuid prefix: `{aizk_uuid}_{normalized_title}.md` if needed

---

## Summary

All NEEDS CLARIFICATION items have been researched and resolved:

1. ✅ Job history retention: Indefinite retention with optional manual purge
2. ✅ S3 bucket quota: Assume unlimited, add monitoring and cost alerts
3. ✅ SQLite vs Postgres: Start with SQLite, document migration path

Three ADRs documented for Phase 1 implementation:

- ADR-001: S3 Storage Strategy and Atomic Finalization
- ADR-002: Idempotency and Payload Version Semantics
- ADR-003: SQLite with WAL Mode for Initial Deployment

Best practices documented for:

- Docling HTML/PDF pipeline configuration
- arXiv content fetching with HTML preference
- GitHub README fetching without API
- Markdown filename normalization for cross-OS compatibility

No blockers identified. Ready to proceed to Phase 1: Data Model and Contracts.
