# Proposal: Health Endpoints

## Intent

The conversion API has no health surface.
Orchestrators (Docker, K8s, systemd) cannot distinguish a healthy instance from one with a broken database connection or unreachable S3 bucket.
This forces operators to discover failures indirectly — when data is missing or requests error — rather than through proactive health monitoring.
Adding liveness and readiness probes closes this gap (remediation plan P0 #3, items 2d + 2e).

## Scope

**In scope:**

- Liveness endpoint confirming the process is running and responsive
- Readiness endpoint validating DB connectivity and S3 reachability
- Structured response body reporting individual check status
- Spec amendment to `conversion-api` for the new requirements

**Out of scope:**

- Worker health signaling (separate concern — worker has no HTTP surface)
- Startup validation / fail-fast on misconfiguration (Phase 3, item 3b)
- Container HEALTHCHECK instruction (Phase 3, item 3d)
- Deep dependency health (e.g., KaraKeep reachability, Litestream status)

## Approach

Add two new endpoints outside the `/v1/` prefix (health probes are infrastructure, not API versioned resources):

- `GET /health/live` — returns 200 if the process is running.
  No dependency checks.
  Suitable for K8s livenessProbe.
- `GET /health/ready` — runs bounded-timeout checks against the database (simple query) and S3 (HEAD bucket).
  Returns 200 when all pass, 503 when any fail.
  Suitable for K8s readinessProbe.

Both endpoints return a JSON body with individual check results for operator visibility.
The readiness checks have per-check timeouts to prevent a slow dependency from hanging the probe.
