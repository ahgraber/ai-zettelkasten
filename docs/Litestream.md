# Litestream replication (SQLite → S3)

Litestream is used to replicate the conversion SQLite database to S3 for durability and recovery. We store replicas in the same bucket as conversion artifacts, under the `db/` prefix (e.g., `s3://aizk/db/<db_filename>`).

## References

- Install: https://litestream.io/install/
- macOS (Homebrew): https://litestream.io/install/mac/
- Getting started: https://litestream.io/getting-started/
- Kubernetes sidecar guide: https://litestream.io/guides/kubernetes/

## Install

macOS (Homebrew):

```bash
brew install benbjohnson/litestream/litestream
```

Linux:

- Use the official install guide for your distro or download the release tarball and place `litestream` on your `PATH`.
- See https://litestream.io/install/ for current packages and binaries.

## Local usage with this repo

Defaults in the conversion service:

- `DATABASE_URL=sqlite:///./data/conversion_service.db`
- `S3_BUCKET_NAME=aizk`
- `LITESTREAM_S3_PREFIX=db`
- `LITESTREAM_START_ROLE=api`

When you start the API (`aizk-conversion serve`) it will:

- generate `./data/litestream.yaml`
- run `litestream restore` if the DB file is missing
- start `litestream replicate` in the background

You can disable this for sidecar deployments with:

```bash
LITESTREAM_ENABLED=false
```

Notes:

- This repo generates a config with an **absolute** database path; if you hand-write the config, use an absolute path to match.
- v0.5+ uses `replica:` (singular) in the config (not legacy `replicas:`).
- For S3-compatible endpoints (MinIO/Garage/etc.), set an explicit `endpoint` in the config. Some providers are auto-detected for `force-path-style` and `sign-payload`; otherwise configure those explicitly.
- Command-line mode requires credentials via environment variables; Litestream auto-reads `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` or `LITESTREAM_ACCESS_KEY_ID`/`LITESTREAM_SECRET_ACCESS_KEY`.
- Command-line mode is single-replica only. For multiple databases or advanced settings, use a config file.

## Litestream bootstrap (existing SQLite DB, v0.5+)

If your SQLite database already exists, you only need to start replication. Litestream creates the initial snapshot and then continuously watches the DB for new changes; restore is only needed when the DB file is missing. The `replicate` command runs as a long-lived process.

1. Ensure `DATABASE_URL` points at the existing file.
2. Start the service (`aizk-conversion serve`) or run Litestream manually:

- `litestream replicate /absolute/path/to/db s3://bucket/prefix`

1. Confirm the bucket contains the DB replica path.

## S3-compatible endpoints (Garage/MinIO)

For S3-compatible providers (e.g., Garage), you may need path-style addressing and payload signing. Set:

```bash
LITESTREAM_S3_FORCE_PATH_STYLE=true
LITESTREAM_S3_SIGN_PAYLOAD=true
```

See https://litestream.io/guides/s3-compatible/ for details.

## Kubernetes sidecar (summary)

Use the Litestream Kubernetes guide for the full manifest examples. The standard pattern is:

- `initContainer` runs `litestream restore` before your app starts.
- A sidecar container runs `litestream replicate`.
- Use a `ConfigMap` for the Litestream config and a `Secret` for S3 credentials.
- Run a **single replica** (StatefulSet with `replicas: 1`) because Litestream expects a single live writer.

The guide is here: https://litestream.io/guides/kubernetes/
