# AI-Zettelkasten

An AI-driven [Zettelkasten](https://zettelkasten.de/introduction/)-style mindmap and assistant for "talk to my data" and deep research over web-based resources.

This project is intended to be self-hosted with minimal infrastructure requirements - a mini PC with a multicore processor and 8+ GB RAM should suffice.
Infrastructure components should be manageable by a 'compose' stack (_note: though I'll be hosting on a k3s cluster_).
This means no GPU requirements; AI inference is provided through API services.

## What is a Zettelkasten?

A Zettelkasten is a way of connecting atomic ideas into a linked (hypertextual), personal knowledge graph.

1. Each Node ("zettel", from German "slip" or "note") is atomic, containing a single concept, idea, or fact
2. Nodes are interconnected with links.
   _A Zettelkasten makes **connecting** and not **collecting** a priority._
3. A Zettelkasten is unique, resulting from knowledge processing over an individual corpus.

Each node must have:

1. A unique address - Defined by a hash based on the content of the note. `xxHash` might be used for an exact hash, `minhash` for textual similarity (i.e., similar words, letters), and/or `semhash` for semantic similarity.
2. Content - The individual (atomic) piece of knowledge.
3. References - The source reference(s) for the content.

In a traditional Zettelkasten, the zettel body would contain links to other nodes.
In the AI Zettelkasten, these are defined as an additional Relationship that contains source/destination directionality, relationship type, and other metadata.

Zettelkasten may also benefit from structural notes that create hierarchy, serving as aggregator or summary nodes about a broader (but still atomic!) concept that incorporates or relates to multiple, more granular nodes.

- [Introduction to the Zettelkasten Method • Zettelkasten Method](https://zettelkasten.de/introduction/)
- [Forget Forgetting. Build a Zettelkasten.](https://every.to/superorganizers/forget-forgetting-build-a-zettelkasten-299960)

## Prerequisites

- [Litestream](https://litestream.io/) (v0.5+): required to replicate the SQLite conversion database to S3 for durability and recovery; we store database replicas in `s3://aizk/db/` alongside conversion artifacts.
- [uv](https://docs.astral.sh/uv/) is recommended to manage the python environment and installation

## Install

This project uses Python 3.12+ and `uv` for dependency management.
To install, clone the repo, then run:

```sh
uv sync
```

## Configure

Configuration is driven by environment variables and `.env` (auto-loaded from the repo root).

Required for API/worker:

- `AIZK_FETCHER__KARAKEEP__API_KEY`
- `AIZK_FETCHER__KARAKEEP__BASE_URL`

Storage (S3 or compatible):

- `S3_BUCKET_NAME` (default `aizk`)
- `S3_ENDPOINT_URL` (required for MinIO/Garage or other S3-compatible endpoints)
- `S3_ACCESS_KEY_ID`
- `S3_SECRET_ACCESS_KEY`
- `S3_REGION` (default `us-east-1`)

Litestream (SQLite replication):

- `LITESTREAM_ENABLED` (default `true`)
- `LITESTREAM_CONFIG_PATH` (default `./data/litestream.yaml`)
- `LITESTREAM_S3_BUCKET_NAME` (optional override; otherwise `S3_BUCKET_NAME`)
- `LITESTREAM_S3_PREFIX` (default `db`)

MLflow tracing (optional):

- `MLFLOW_TRACING_ENABLED` (default `false`)
- `MLFLOW_TRACKING_URI` (optional; uses MLflow defaults when unset)
- `MLFLOW_EXPERIMENT_NAME` (optional)

Deployment trust model:

- `AIZK_AUTH_MODE` (default `trust_network`).
  At this build the only implemented mode is `trust_network`, which treats every inbound request as authenticated and stamps a single deployment-wide principal on every row.
  Reserved literal values `token`, `proxy_headers`, and `oidc` are rejected at startup with a typed `ConfigurationError` so the process refuses to boot rather than silently defaulting to an unintended trust posture.
  Each future mode lands as a delta on the `Principal.provenance` literal, the `AuthSettings.auth_mode` validator, and the `get_principal` resolver match — no schema migration required.
- `AIZK_DEFAULT_PRINCIPAL` (default `self`).
  In `trust_network` mode this string is the `subject` of every resolved `Principal` and therefore the `owner_id` written to `sources`, `conversion_jobs`, and `conversion_outputs` rows.
  Override per deployment to attribute rows to a named operator or service account.
- `AIZK_TRUSTED_HOSTS` (default `["localhost", "127.0.0.1"]`, JSON array).
  Allowlist for the `Host` header that reaches the API process; mismatches return HTTP 400 (`Invalid host header`) before any route handler runs.
  **Operators MUST override this for any non-localhost deployment.**
  The middleware checks the actual `Host` the API process sees — `Forwarded` and `X-Forwarded-Host` are intentionally NOT consulted, so the reverse proxy is responsible for (a) rewriting `Host` to a value in this allowlist on the way in and (b) stripping any client-supplied `X-Forwarded-Host` so it cannot reach the API.
  Wildcards are supported, e.g. `["*.internal.example.com"]`.

Docs: see `docs/Litestream.md` for full setup and sidecar guidance.

## Running `aizk`

Run the conversion CLI with uv:

```sh
uv run aizk-conversion db-init
AIZK_FETCHER__KARAKEEP__API_KEY=... AIZK_FETCHER__KARAKEEP__BASE_URL=... uv run aizk-conversion serve
AIZK_FETCHER__KARAKEEP__API_KEY=... AIZK_FETCHER__KARAKEEP__BASE_URL=... uv run aizk-conversion worker
```

### Backfill KaraKeep bookmarks

To backfill or re-enqueue existing KaraKeep bookmarks, use the notebook at `notebooks/karakeep_conversion_pipeline.py`.
It pages through KaraKeep and submits bookmark IDs to the conversion API.
The notebook includes required env vars, startup commands for API/worker, and a `KARAKEEP_DRY_RUN` mode for verification.

## Design

### Data Flow

1. Collect: use [Karakeep](https://karakeep.app/) as a content management system for bookmarking and archiving web-based resources.
   Karakeep archives content and extracts text content when possible, but specialized content extraction & parsing will perform better for archived files (such as PDFs from arxiv.org).
2. Parse: Extract, and clean content with [docling-project/docling](https://github.com/docling-project/docling/tree/main).
   Export markdown and extracted images to S3-compatible blob storage.
3. Chunk
4. Embed
5. Index
6. Retrieve (search, rerank)
7. Respond
8. Research
9. Explore

## Devshell

This repo uses nix devshells to manage project dependencies.

Use node2nix to create `node-env.nix` from `package.json` `node-env.nix` will be picked up in the flake devshell

```sh
node2nix -i package.json -o ./nix/node-packages.nix -c ./nix/default.nix -e ./nix/node-env.nix -18
```

## Containers (Podman)

Use the Podman compose file to run API + worker separately from the same image:

```sh
podman-compose -f containers/podman-compose.yaml up -d --build
```

## AI Disclosure

This project uses spec-driven development to allow AI coding assistance to work on well-specified features.
See the `sdd-*` family of [ahgraber/skills: Agent skills](https://github.com/ahgraber/skills).

## Testing

Run tests with uv:

```sh
uv run pytest tests/
```

Run tests in parallel across CPU cores using [pytest-xdist](https://pytest-xdist.readthedocs.io/en/stable/):

```sh
uv run pytest -n auto -m "not integration_lifecycle" tests/
```

Subprocess lifecycle tests (`integration_lifecycle`) are incompatible with xdist and must be run separately:

```sh
uv run pytest -m integration_lifecycle tests/
```

With coverage:

```sh
uv run pytest -n auto -m "not integration_lifecycle" --cov=src --cov-report=term-missing tests/
```

## Contributing

Contributions and fixes are welcome.
Please open issues or pull requests with clear descriptions and tests where appropriate.

### Releasing

This project uses [uv-ship](https://github.com/floRaths/uv-ship) to manage releases.
Install it as a uv tool:

```bash
uv tool install uv-ship
```

To cut a release:

```bash
# do a dry run first!
uv-ship --dry-run next <major | minor | patch>

# if everything looks good, ship it
uv-ship next <major | minor | patch>
```

This bumps the version in `pyproject.toml`, updates `CHANGELOG`, commits, tags, and pushes.
See `[tool.uv-ship]` in `pyproject.toml` for configuration.
