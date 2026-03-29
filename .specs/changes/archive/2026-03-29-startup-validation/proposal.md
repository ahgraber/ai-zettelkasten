# Proposal: Startup Validation

## Intent

Operators currently receive no feedback when external services are misconfigured or optional features are silently disabled.
Misconfigured S3 credentials, unreachable KaraKeep endpoints, or disabled features (picture descriptions, MLflow, Litestream) are discovered hours later when expected data is missing.
Both the worker and API processes should validate required service reachability at startup and log a clear summary of enabled/disabled optional features so misconfigurations are caught immediately.

## Scope

**In scope:**

- Startup-time reachability probes for required external services (S3 bucket, KaraKeep API)
- Startup log summary of optional feature status (picture descriptions, MLflow tracing, Litestream replication)
- Fail-fast behavior when required services are unreachable
- Both worker and API process entry points

**Out of scope:**

- Runtime health checks (already implemented via `/health/ready`)
- Continuous connectivity monitoring or circuit breakers
- New API endpoints or configuration schema changes
- Validation of Docling or other local-only dependencies

## Approach

Add a startup validation module (`aizk/conversion/utilities/startup.py`) that:

1. Probes required services (S3 HEAD bucket, KaraKeep API health/ping) with bounded timeouts
2. Logs a structured summary of optional feature states (enabled/disabled with reason)
3. Raises a fatal error on required service failure, preventing the process from starting

Wire the module into both `_cmd_worker()` and `_cmd_serve()` in `cli.py`, after config is loaded and before the main loop or uvicorn starts.

## Schema Impact

None.
This change adds no API endpoints, request/response models, or database schema changes.
