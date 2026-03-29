# Delta for Conversion API

## ADDED Requirements

### Requirement: Expose liveness probe

The system SHALL expose a liveness endpoint that returns HTTP 200 when the API process is running and responsive, without checking any external dependencies.

**Schema reference:** `GET /health/live` · response: `HealthResponse`

#### Scenario: Liveness check succeeds

- **GIVEN** the API process is running
- **WHEN** a client requests the liveness endpoint
- **THEN** the system returns HTTP 200 with status "ok"

### Requirement: Expose readiness probe with dependency checks

The system SHALL expose a readiness endpoint that validates database connectivity and S3 reachability, returning HTTP 200 when all checks pass and HTTP 503 when any check fails.

**Schema reference:** `GET /health/ready` · response: `HealthResponse`

#### Scenario: All dependencies healthy

- **GIVEN** the database is reachable and S3 credentials are valid
- **WHEN** a client requests the readiness endpoint
- **THEN** the system returns HTTP 200 with status "ok" and individual check results showing each dependency as healthy

#### Scenario: Database unreachable

- **GIVEN** the database connection fails or times out
- **WHEN** a client requests the readiness endpoint
- **THEN** the system returns HTTP 503 with status "unavailable" and the database check result includes the failure reason

#### Scenario: S3 unreachable

- **GIVEN** S3 returns an error or times out on a HEAD bucket request
- **WHEN** a client requests the readiness endpoint
- **THEN** the system returns HTTP 503 with status "unavailable" and the S3 check result includes the failure reason

#### Scenario: Multiple dependencies unhealthy

- **GIVEN** both the database and S3 are unreachable
- **WHEN** a client requests the readiness endpoint
- **THEN** the system returns HTTP 503 with all failing check results reported — checks are not short-circuited

### Requirement: Bound readiness check duration

The system SHALL enforce a per-check timeout on each readiness dependency check to prevent a slow or unresponsive dependency from hanging the probe response.

#### Scenario: Dependency check exceeds timeout

- **GIVEN** a dependency check does not complete within its timeout
- **WHEN** the readiness endpoint is evaluating checks
- **THEN** the timed-out check is reported as unhealthy with a timeout indication and the overall response is HTTP 503
