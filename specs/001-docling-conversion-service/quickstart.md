# Quickstart: Docling Conversion Service

**Feature**: 001-docling-conversion-service
**Date**: 2025-12-23
**Audience**: Developers setting up and using the conversion service

## Overview

The Docling Conversion Service converts KaraKeep bookmarks (HTML, PDF, arXiv, GitHub) to Markdown with figure extraction. It provides a REST API and Web UI for job management.

**Key Features**:

- REST API for job submission and monitoring
- Durable SQLite job queue with retry logic
- S3 storage for Markdown and extracted figures
- Idempotent processing with reprocessing support
- HTML-only Web UI for operational visibility

## Prerequisites

- Python 3.12+ with uv installed
- S3-compatible storage (AWS S3, Backblaze B2, Garage, MinIO, etc.)
- 8GB RAM minimum (for 4 concurrent Docling workers)
- 10GB disk space (for temp workspace)

## Quick Start (Local Development)

### 1. Clone and Setup Environment

```bash
# Navigate to repository root
cd /Users/mithras/_code/ai-zettelkasten

# Activate Nix devshell (if using Nix)
nix develop

# Or activate virtual environment via uv
source .venv/bin/activate

# Verify Python version
python --version  # Should be 3.12+
```

### 2. Configure Environment Variables

Create `.env` file in repository root:

```bash
# Database
DATABASE_URL=sqlite:///./data/conversion_service.db

# S3 Storage
S3_ENDPOINT_URL=http://localhost:9000  # Local S3-compatible service for dev
S3_BUCKET_NAME=aizk-conversions
S3_ACCESS_KEY_ID=""
S3_SECRET_ACCESS_KEY=""
S3_REGION=us-east-1

# Service Configuration
QUEUE_MAX_DEPTH=1000
WORKER_CONCURRENCY=4
FETCH_TIMEOUT_SECONDS=30
RETRY_MAX_ATTEMPTS=3
RETRY_BASE_DELAY_SECONDS=60

# Docling Configuration
DOCLING_PDF_MAX_PAGES=100
DOCLING_ENABLE_OCR=true
DOCLING_ENABLE_TABLE_STRUCTURE=true

# Logging
LOG_LEVEL=INFO
LOG_FORMAT=json  # or 'text' for human-readable

# API Server
API_HOST=0.0.0.0
API_PORT=8000
API_RELOAD=true  # Enable auto-reload in development
```

### 3. Start Local S3-Compatible Storage

```bash
# Example using MinIO (you can use Garage, LocalStack, or others)
docker run -d \
  -p 9000:9000 \
  -p 9001:9001 \
  --name s3-local \
  -e "MINIO_ROOT_USER=admin" \
  -e "MINIO_ROOT_PASSWORD=password" \
  minio/minio server /data --console-address ":9001"

# Create bucket (using MinIO client for this example)
docker exec s3-local mc alias set local http://localhost:9000 admin password
docker exec s3-local mc mb local/aizk-conversions

# Web console: http://localhost:9001 (admin / password)
# Note: Adjust commands for your specific S3-compatible service
```

### 4. Initialize Database

```bash
# Run database migrations (creates tables and indexes)
python -m aizk.conversion.cli db init

# Verify database created
ls -lh data/conversion_service.db
```

### 5. Start Conversion Service

```bash
# Start FastAPI server with workers
python -m aizk.conversion.cli serve

# Or use uvicorn directly
uvicorn aizk.conversion.api.main:app --host 0.0.0.0 --port 8000 --reload
```

**Service endpoints**:

- API: http://localhost:8000
- API docs (Swagger): http://localhost:8000/docs
- Web UI: http://localhost:8000/ui/jobs
- Health check: http://localhost:8000/health

### 6. Submit Test Job

```bash
# Using curl
curl -X POST http://localhost:8000/v1/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "karakeep_id": "test_001",
    "url": "https://arxiv.org/abs/1706.03762",
    "title": "Attention Is All You Need"
  }'

# Using httpie
http POST localhost:8000/v1/jobs \
  karakeep_id=test_001 \
  url=https://arxiv.org/abs/1706.03762 \
  title="Attention Is All You Need"

# Using Python
import requests

response = requests.post("http://localhost:8000/v1/jobs", json={
    "karakeep_id": "test_001",
    "url": "https://arxiv.org/abs/1706.03762",
    "title": "Attention Is All You Need"
})
print(response.json())
```

**Expected response**:

```json
{
  "id": 1,
  "aizk_uuid": "550e8400-e29b-41d4-a716-446655440000",
  "karakeep_id": "test_001",
  "url": "https://arxiv.org/abs/1706.03762",
  "title": "Attention Is All You Need",
  "source_type": "arxiv",
  "status": "QUEUED",
  "attempts": 0,
  "payload_version": 1,
  "idempotency_key": "abc123...",
  "created_at": "2025-12-23T10:00:00Z",
  "updated_at": "2025-12-23T10:00:00Z"
}
```

### 7. Monitor Job Progress

```bash
# Get job status
curl http://localhost:8000/v1/jobs/1

# List all jobs
curl http://localhost:8000/v1/jobs

# Filter by status
curl "http://localhost:8000/v1/jobs?status=SUCCEEDED"

# View in Web UI
open http://localhost:8000/ui/jobs
```

### 8. Retrieve Conversion Output

```bash
# Get outputs for bookmark (using aizk_uuid from job response)
curl http://localhost:8000/v1/outputs/550e8400-e29b-41d4-a716-446655440000

# Get only latest output
curl "http://localhost:8000/v1/outputs/550e8400-e29b-41d4-a716-446655440000?latest=true"
```

**Expected response**:

```json
{
  "aizk_uuid": "550e8400-e29b-41d4-a716-446655440000",
  "outputs": [
    {
      "id": 1,
      "job_id": 1,
      "aizk_uuid": "550e8400-e29b-41d4-a716-446655440000",
      "payload_version": 1,
      "s3_prefix": "s3://aizk-conversions/550e8400-e29b-41d4-a716-446655440000/",
      "markdown_key": "s3://aizk-conversions/550e8400-e29b-41d4-a716-446655440000/attention-is-all-you-need.md",
      "manifest_key": "s3://aizk-conversions/550e8400-e29b-41d4-a716-446655440000/manifest.json",
      "markdown_hash_xx64": "1234567890abcdef",
      "figure_count": 8,
      "docling_version": "2.65.0",
      "pipeline_name": "html",
      "created_at": "2025-12-23T10:02:15Z"
    }
  ]
}
```

### 9. Download Artifacts from S3

```bash
# Using AWS CLI (with S3-compatible endpoint)
aws --endpoint-url http://localhost:9000 \
  s3 cp s3://aizk-conversions/550e8400-e29b-41d4-a716-446655440000/attention-is-all-you-need.md \
  ./output.md

# Download manifest
aws --endpoint-url http://localhost:9000 \
  s3 cp s3://aizk-conversions/550e8400-e29b-41d4-a716-446655440000/manifest.json \
  ./manifest.json

# Download all artifacts
aws --endpoint-url http://localhost:9000 \
  s3 sync s3://aizk-conversions/550e8400-e29b-41d4-a716-446655440000/ \
  ./output/
```

## Common Workflows

### Batch Job Submission

```bash
# Submit multiple jobs at once
curl -X POST http://localhost:8000/v1/jobs/batch \
  -H "Content-Type: application/json" \
  -d '{
    "jobs": [
      {
        "karakeep_id": "bm_001",
        "url": "https://arxiv.org/abs/1706.03762",
        "title": "Attention Paper"
      },
      {
        "karakeep_id": "bm_002",
        "url": "https://github.com/microsoft/docling",
        "title": "Docling Repo"
      }
    ]
  }'
```

**Response includes per-item status**:

```json
{
  "results": [
    {"index": 0, "status": "created", "job": {...}},
    {"index": 1, "status": "created", "job": {...}}
  ],
  "summary": {
    "created": 2,
    "duplicates": 0,
    "errors": 0
  }
}
```

### Retry Failed Jobs

```bash
# Retry single job
curl -X POST http://localhost:8000/v1/jobs/1/retry

# Bulk retry (Web UI or API)
curl -X POST http://localhost:8000/v1/jobs/actions \
  -H "Content-Type: application/json" \
  -d '{
    "action": "retry",
    "job_ids": [1, 2, 3]
  }'
```

### Reprocess with New Pipeline Version

```bash
# Submit same bookmark with incremented payload_version
curl -X POST http://localhost:8000/v1/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "karakeep_id": "test_001",
    "url": "https://arxiv.org/abs/1706.03762",
    "title": "Attention Is All You Need",
    "payload_version": 2
  }'

# New job created despite existing output
# If Markdown content differs, new artifacts written to S3
# If identical (same markdown_hash_xx64), existing S3 location reused
```

### Cancel Running Jobs

```bash
# Cancel single job
curl -X POST http://localhost:8000/v1/jobs/1/cancel

# Bulk cancel
curl -X POST http://localhost:8000/v1/jobs/actions \
  -H "Content-Type: application/json" \
  -d '{
    "action": "cancel",
    "job_ids": [1, 2, 3]
  }'
```

## Web UI Usage

Access Web UI at http://localhost:8000/ui/jobs

**Features**:

- View all jobs in table with sortable columns
- Filter by status (QUEUED, RUNNING, SUCCEEDED, FAILED, CANCELLED)
- Text search by aizk_uuid, karakeep_id, or title
- Multi-select jobs with checkboxes
- Bulk retry or cancel selected jobs
- View error details for failed jobs
- Click job ID to view full details

## Development Tips

### Watch Logs

```bash
# Tail service logs (JSON format)
tail -f logs/conversion_service.log | jq

# Filter by job ID
tail -f logs/conversion_service.log | jq 'select(.job_id == 1)'

# Filter by error level
tail -f logs/conversion_service.log | jq 'select(.level == "ERROR")'
```

### Database Inspection

```bash
# SQLite CLI
sqlite3 data/conversion_service.db

# Common queries
SELECT id, status, attempts, error_code FROM conversion_jobs ORDER BY created_at DESC LIMIT 10;
SELECT status, COUNT(*) FROM conversion_jobs GROUP BY status;
SELECT * FROM bookmarks WHERE karakeep_id = 'test_001';
```

### Reset Database

```bash
# Stop service first!

# Delete database
rm data/conversion_service.db

# Recreate
python -m aizk.conversion.cli db init
```

### Clear S3 Bucket

```bash
# Delete all objects using AWS CLI
aws --endpoint-url http://localhost:9000 \
  s3 rm s3://aizk-conversions/ --recursive

# Or use your S3 service's web console
```

## Testing

### Run Test Suite

```bash
# All tests
pytest tests/conversion/

# Specific test module
pytest tests/conversion/test_api.py

# With coverage
pytest --cov=aizk.conversion tests/conversion/

# Integration tests (require S3)
pytest tests/conversion/integration/ -v
```

### Test Fixtures

```python
# Example test using fixtures
import pytest
from aizk.conversion.models import Bookmark


def test_job_submission(client, test_bookmark):
    """Test job submission API."""
    response = client.post(
        "/v1/jobs",
        json={"karakeep_id": test_bookmark.karakeep_id, "url": test_bookmark.url, "title": test_bookmark.title},
    )
    assert response.status_code == 201
    assert response.json()["status"] == "QUEUED"
```

## Troubleshooting

### Job Stuck in QUEUED Status

**Cause**: Worker not running or at max concurrency

**Solution**:

```bash
# Check worker count in logs
grep "worker_id" logs/conversion_service.log

# Increase concurrency in .env
WORKER_CONCURRENCY=8

# Restart service
```

### S3 Upload Failures

**Cause**: Invalid credentials or bucket doesn't exist

**Solution**:

```bash
# Verify S3 credentials
aws --endpoint-url http://localhost:9000 s3 ls

# Check bucket exists
aws --endpoint-url http://localhost:9000 s3 ls s3://aizk-conversions/

# Verify .env settings match MinIO configuration
```

### Docling Conversion Errors

**Cause**: Malformed PDF, missing dependencies, or memory exhaustion

**Solution**:

```bash
# Check Docling version
python -c "import docling; print(docling.__version__)"

# Test Docling manually
python -m aizk.conversion.cli test-docling --url "https://arxiv.org/abs/1706.03762"

# Increase memory if OOM
# Reduce WORKER_CONCURRENCY or DOCLING_PDF_MAX_PAGES
```

### Database Lock Errors

**Cause**: WAL mode not enabled or excessive write contention

**Solution**:

```bash
# Verify WAL mode
sqlite3 data/conversion_service.db "PRAGMA journal_mode;"
# Should return: wal

# Enable WAL if not set
sqlite3 data/conversion_service.db "PRAGMA journal_mode=WAL;"

# Reduce worker concurrency if lock timeouts persist
WORKER_CONCURRENCY=2
```

## Docker Compose Deployment

### docker-compose.yml

```yaml
version: '3.8'

services:
  conversion-service:
    build: .
    ports:
      - 8000:8000
    environment:
      DATABASE_URL: sqlite:////data/conversion_service.db
      S3_ENDPOINT_URL: http://s3:9000
      S3_BUCKET_NAME: aizk-conversions
      S3_ACCESS_KEY_ID: minioadmin
      S3_SECRET_ACCESS_KEY: minioadmin
      WORKER_CONCURRENCY: 4
    volumes:
      - ./data:/data
      - ./logs:/logs
    depends_on:
      - s3

  s3:
    image: minio/minio  # or quay.io/minio/minio, or another S3-compatible service
    ports:
      - 9000:9000
      - 9001:9001
    environment:
      MINIO_ROOT_USER: admin
      MINIO_ROOT_PASSWORD: password
    command: server /data --console-address ":9001"
    volumes:
      - s3_data:/data

volumes:
  s3_data:
```

### Start Stack

```bash
# Build and start
docker-compose up -d

# View logs
docker-compose logs -f conversion-service

# Stop
docker-compose down
```

## Production Considerations

### Environment Variables (Production)

```bash
# Use production S3
S3_ENDPOINT_URL=https://s3.amazonaws.com
S3_BUCKET_NAME=prod-aizk-conversions
S3_ACCESS_KEY_ID=<from-secrets-manager>
S3_SECRET_ACCESS_KEY=<from-secrets-manager>

# Adjust for higher throughput
WORKER_CONCURRENCY=16
QUEUE_MAX_DEPTH=5000

# Stricter logging
LOG_LEVEL=WARNING
LOG_FORMAT=json

# Disable auto-reload
API_RELOAD=false
```

### Monitoring

- **Metrics**: Emit to Prometheus/CloudWatch

  - Queue depth gauge
  - Job duration histogram
  - Success/failure rate counters
  - S3 upload latency histogram

- **Alerts**: Set up based on:

  - Queue depth > 80% of max
  - Job failure rate > 10%
  - Worker crashes or restarts

- **Logs**: Ship structured JSON logs to:

  - CloudWatch Logs
  - ELK stack
  - Splunk

### Backup Strategy

```bash
# Database backup (use WAL checkpoint first)
sqlite3 data/conversion_service.db "PRAGMA wal_checkpoint(FULL);"
cp data/conversion_service.db backups/conversion_service_$(date +%Y%m%d).db

# S3 artifacts (enable versioning on bucket)
aws s3api put-bucket-versioning \
  --bucket prod-aizk-conversions \
  --versioning-configuration Status=Enabled
```

### Migration to PostgreSQL (If Needed)

See `docs/decision-record/ADR-003-sqlite-wal-mode.md` for migration guide when scale exceeds SQLite capabilities (>8 workers, >10GB database).

## Next Steps

1. Review [data-model.md](./data-model.md) for complete schema details
2. Review [contracts/openapi.yaml](./contracts/openapi.yaml) for full API specification
3. Review [research.md](./research.md) for architectural decisions
4. Read ADRs in `docs/decision-record/` for rationale behind key choices
5. Proceed to implementation with `/speckit.tasks` command

## Support

For issues or questions:

- Check troubleshooting section above
- Review logs with structured logging context (aizk_uuid, job_id)
- Consult API docs at http://localhost:8000/docs
- Refer to constitution gates in [plan.md](./plan.md)
