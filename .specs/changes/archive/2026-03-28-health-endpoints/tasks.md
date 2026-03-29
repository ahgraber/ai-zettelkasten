# Tasks: Health Endpoints

## Response Schemas

- [x] Add `CheckResult` and `HealthResponse` pydantic models to API schemas
- [x] Add tests for schema serialization (ok/unavailable states, null detail)

## Health Router

- [x] Create `src/aizk/conversion/api/routes/health.py` with `health_router`
- [x] Implement `GET /health/live` returning 200 with `HealthResponse(status="ok", checks=[])`
- [x] Implement `GET /health/ready` running DB and S3 checks concurrently via `asyncio.gather`
- [x] Implement `_check_db`: `SELECT 1` via `asyncio.to_thread` with `asyncio.wait_for` timeout
- [x] Implement `_check_s3`: `head_bucket` via `asyncio.to_thread` with `asyncio.wait_for` timeout
- [x] Return 200 when all checks pass, 503 when any fail, with individual `CheckResult` entries

## Router Registration

- [x] Export `health_router` from `routes/__init__.py`
- [x] Register `health_router` in `main.py` `create_app()`

## Tests

- [x] Test liveness endpoint returns 200 with status "ok"
- [x] Test readiness endpoint returns 200 when DB and S3 are healthy
- [x] Test readiness endpoint returns 503 with check details when DB is unreachable
- [x] Test readiness endpoint returns 503 with check details when S3 is unreachable
- [x] Test readiness endpoint returns 503 with both failures when both are unreachable
- [x] Test readiness check timeout is enforced (mock a slow dependency)
