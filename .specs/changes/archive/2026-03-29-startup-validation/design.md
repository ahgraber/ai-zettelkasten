# Design: Startup Validation

## Context

The conversion service has two process entry points (`_cmd_serve` and `_cmd_worker` in `cli.py`) that both load `ConversionConfig` and start external service integrations.
Health check endpoints (`/health/ready`) already probe S3 and DB at runtime, but there is no equivalent validation at startup.
The `_require_karakeep_env()` function checks that env vars _exist_ but not that the service is _reachable_.

Optional features (picture descriptions, MLflow, Litestream) degrade silently when unconfigured — operators discover gaps only when expected data is missing.

## Decisions

### Decision: Single validation function shared by both entry points

**Chosen:** A `validate_startup(config)` function in `aizk/conversion/utilities/startup.py` called from both `_cmd_serve()` and `_cmd_worker()`.

**Rationale:** Both processes depend on the same external services.
Duplicating validation logic would drift.
The function takes `ConversionConfig` and a role string to tailor logging.

**Alternatives considered:**

- Separate validation per entry point: simpler initially but guarantees drift as processes evolve.
- Validation inside `ConversionConfig.__init__()`: conflates config parsing with I/O; breaks testability and makes config construction do network calls.

### Decision: Required probes are synchronous and blocking

**Chosen:** Run S3 HEAD bucket and KaraKeep health probe synchronously at startup, before the event loop or worker loop starts.

**Rationale:** Startup is inherently sequential.
The process should not accept work until services are validated.
Async adds complexity with no benefit here — neither uvicorn nor the worker loop is running yet when validation executes.

**Alternatives considered:**

- Async probes with `asyncio.run()`: unnecessary complexity for two sequential HTTP calls at startup.
- Background probes after startup: defeats the purpose — the process would start accepting work before validation completes.

### Decision: Reuse existing S3Client for the S3 probe

**Chosen:** Instantiate `S3Client(config)` and call `head_bucket()` — the same pattern used by the readiness check.

**Rationale:** Avoids duplicating boto3 client construction.
The `S3Client` already handles credentials and endpoint configuration.

**Alternatives considered:**

- Raw boto3 call: duplicates client setup logic already in `S3Client`.

### Decision: KaraKeep probe uses a lightweight API call

**Chosen:** HTTP GET to `{KARAKEEP_BASE_URL}/api/v1/bookmarks?limit=1` with the API key header and a bounded timeout.

**Rationale:** KaraKeep has no dedicated health endpoint.
A minimal bookmarks list request validates both reachability and authentication with minimal overhead.
Using `limit=1` keeps the response small.

**Alternatives considered:**

- HEAD request to base URL: doesn't validate API key authentication.
- Dedicated health endpoint: would require KaraKeep changes outside our control.

### Decision: Optional features are logged, not validated

**Chosen:** Log a structured summary of enabled/disabled optional features at INFO level.
Do not fail startup for disabled optional features.

**Rationale:** Optional features are optional by definition.
Failing startup because MLflow is unconfigured would break local development.
The goal is _visibility_, not enforcement.

**Alternatives considered:**

- WARN level for each disabled feature: too noisy in development where most features are intentionally disabled.
  A single summary at INFO is sufficient.

## Architecture

```text
cli.py
  _cmd_serve() / _cmd_worker()
    |
    v
  ConversionConfig()          # parse env vars
    |
    v
  validate_startup(config)    # NEW — from startup.py
    |
    +---> probe_s3(config)          # HEAD bucket, 10s timeout
    |       fail? => log error, sys.exit(1)
    |
    +---> probe_karakeep(config)    # GET bookmarks?limit=1, 10s timeout
    |       fail? => log error, sys.exit(1)
    |
    +---> log_feature_summary(config)  # INFO log with feature states
    |
    v
  (existing startup: MLflow, Litestream, migrations, run)
```

## Risks

- **KaraKeep API changes break the probe:** Mitigated by using a stable, paginated list endpoint rather than an undocumented health path.
  If the endpoint changes, the probe fails loudly — which is the correct behavior.
- **Slow probes delay startup:** Mitigated by a 10-second timeout per probe.
  Worst case adds 20 seconds to startup if both services are slow but eventually reachable.
- **S3 probe fails in local dev without S3:** Mitigated by making the probe respect the existing config — if `s3_endpoint_url` is empty and defaults are used, the probe runs against the configured endpoint.
  Local dev with MinIO will work; truly offline dev will fail fast with a clear message.
