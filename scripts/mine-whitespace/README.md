# mine-whitespace

Samples real conversion outputs from S3 and extracts whitespace-interesting
excerpts for embedding in `tests/conversion/unit/test_whitespace_real_world.py`.

## Prerequisites

S3 credentials in the environment (or `.env`):

```text
S3_ENDPOINT_URL=...
S3_ACCESS_KEY_ID=...
S3_SECRET_ACCESS_KEY=...
S3_BUCKET_NAME=aizk      # default
S3_REGION=us-east-1      # default
```

Local SQLite DB at `data/conversion_service.db`.

## Usage

**Find and inspect high-scoring documents (JSON):**

```sh
uv run scripts/mine-whitespace/sample_whitespace_patterns.py \
    --min-score 5 > /tmp/interesting.json
```

**Print excerpts as `repr()` strings ready to paste into the test file:**

```sh
uv run scripts/mine-whitespace/sample_whitespace_patterns.py \
    --min-score 5 --repr
```

Copy a printed excerpt into `test_whitespace_real_world.py` as a new string
constant, then add a test class following the existing pattern.

## Flags

| Flag             | Default                      | Description                                          |
| ---------------- | ---------------------------- | ---------------------------------------------------- |
| `--batch-size N` | 50                           | Documents to sample (split evenly html/pdf)          |
| `--offset N`     | 0                            | Skip first N rows (for batching through the full DB) |
| `--min-score N`  | 0                            | Only include documents with composite score ≥ N      |
| `--db PATH`      | `data/conversion_service.db` | Path to SQLite DB                                    |
| `--repr`         | off                          | Output `repr()` strings instead of JSON              |

The composite score weights multi-spaces (×2), excess newlines (×3), and trailing-whitespace lines (×1).
