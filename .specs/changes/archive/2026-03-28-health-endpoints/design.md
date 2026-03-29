# Design: Health Endpoints

## Context

The conversion API is a FastAPI application served by uvicorn.
Configuration is loaded once at startup into `app.state.config`.
Database sessions and S3 clients are created per-request via FastAPI dependency injection (`dependencies.py`).
The API currently has four routers (`/v1/jobs`, `/v1/bookmarks`, `/v1/outputs`, `/ui`), all under versioned or purpose-specific prefixes.

Health endpoints must work for container orchestrators (Docker HEALTHCHECK, K8s probes) which expect fast, low-overhead HTTP checks with clear pass/fail semantics.

## Decisions

### Decision: Route prefix `/health/` outside versioned API

**Chosen:** Health endpoints live under `/health/live` and `/health/ready`, not under `/v1/`.

**Rationale:** Health probes are infrastructure concerns, not API resources.
They should not change when the API version changes.
Orchestrators hardcode probe paths — versioning them creates unnecessary coupling.

**Alternatives considered:**

- `/v1/health/*`: Ties probe paths to API versioning; forces orchestrator config updates on version bumps.
- Root-level `/healthz`, `/readyz`: K8s convention but less self-documenting; `/health/` prefix groups both probes cleanly.

### Decision: Readiness checks run concurrently with per-check timeouts

**Chosen:** Run DB and S3 checks concurrently using `asyncio.gather`.
Each check has its own timeout (default 5s).
The overall readiness response includes all check results regardless of individual pass/fail.

**Rationale:** Running checks serially doubles worst-case probe latency.
Short-circuiting on first failure hides the state of other dependencies from operators.
Per-check timeouts prevent a single slow dependency from consuming the orchestrator's overall probe timeout.

**Alternatives considered:**

- Serial checks: Simpler but doubles latency when both dependencies are slow.
- Single overall timeout: Doesn't distinguish which dependency is slow; harder to tune.
- Short-circuit on first failure: Faster 503 but hides whether other dependencies are also down.

### Decision: Separate router module

**Chosen:** Add `src/aizk/conversion/api/routes/health.py` as a new router, registered in `main.py` alongside existing routers.

**Rationale:** Follows existing pattern (one router per concern).
Keeps health check logic isolated from job/bookmark/output routes.

**Alternatives considered:**

- Inline in `main.py`: Breaks the established router-per-concern pattern.
  Health checks will grow if more dependencies are added later.

### Decision: DB check via `SELECT 1`, S3 check via HEAD bucket

**Chosen:** Database health is verified with `SELECT 1` executed through SQLAlchemy.
S3 health is verified with `head_bucket()` on the configured bucket.

**Rationale:** Both are the cheapest possible operations that validate connectivity and credentials. `SELECT 1` confirms the SQLite file is accessible and the engine is functional. `head_bucket` confirms credentials, endpoint reachability, and bucket existence in a single call.

**Alternatives considered:**

- DB: `SELECT COUNT(*) FROM conversion_jobs` — unnecessary table scan; conflates schema state with connectivity.
- S3: `list_objects(MaxKeys=1)` — works but more expensive and requires different IAM permissions than HEAD.

### Decision: Synchronous DB check wrapped in async executor

**Chosen:** The SQLAlchemy `SELECT 1` is a synchronous call.
Wrap it in `asyncio.to_thread()` to avoid blocking the event loop, then apply `asyncio.wait_for()` for the timeout.

**Rationale:** The existing DB layer uses synchronous SQLAlchemy (not async).
Converting to async SQLAlchemy is out of scope.
`to_thread` is the standard pattern for running sync I/O in an async context without blocking other requests.

**Alternatives considered:**

- Run synchronously in the handler: Blocks the event loop during the check, affecting concurrent request handling.
- Convert to async SQLAlchemy: Large scope change unrelated to health endpoints.

## Architecture

```text
GET /health/live
  → Return 200 { status: "ok" }

GET /health/ready
  → asyncio.gather(
       check_db(engine, timeout=5s),
       check_s3(s3_client, timeout=5s),
     )
  → All pass → 200 { status: "ok", checks: [...] }
  → Any fail → 503 { status: "unavailable", checks: [...] }
```

Response schema:

```text
HealthResponse {
  status: "ok" | "unavailable"
  checks: CheckResult[]   # empty for liveness
}

CheckResult {
  name: str               # "database" | "s3"
  status: "ok" | "unavailable"
  detail: str | null      # error message on failure, null on success
}
```

## Risks

- **S3 check latency under network issues**: HEAD bucket to a remote S3 endpoint could be slow.
  Mitigated by per-check timeout (5s default).
  Orchestrators should set their probe timeout slightly above this.
- **False-negative readiness on transient errors**: A single failed check marks the instance as not-ready, potentially causing orchestrator traffic shifts.
  Acceptable — K8s readiness probes have configurable `failureThreshold` (default 3) to tolerate transient blips.
