# Tasks: deployment-trust-model

## Settings additions

- [x] Locate the existing API settings model (likely `src/aizk/conversion/api/settings.py` or `src/aizk/conversion/settings.py` — confirm by grepping for the existing `AIZK_*` field declarations).
  Found at `src/aizk/conversion/utilities/config.py` (`ConversionConfig`); existing fields use `validation_alias` with bare env names, while sub-configs (`DoclingConverterConfig`, `IngressPolicy`) use `AIZK_*__*` nested prefixes.
- [x] Add `auth_mode: Literal["trust_network", "token", "proxy_headers", "oidc"] = "trust_network"` field to the settings model with env-var binding to `AIZK_AUTH_MODE`.
  Placed on a new `AuthSettings` sub-model (per user direction); literal env-var name preserved via `validation_alias="AIZK_AUTH_MODE"`.
- [x] Add `default_principal: str = "local"` field to the settings model with env-var binding to `AIZK_DEFAULT_PRINCIPAL`.
  On `AuthSettings`; `validation_alias="AIZK_DEFAULT_PRINCIPAL"`.
- [x] Add `trusted_hosts: list[str] = ["localhost", "127.0.0.1"]` field to the settings model with env-var binding to `AIZK_TRUSTED_HOSTS`.
  Placed on `ConversionConfig` (network host enforcement, not auth identity).
  Uses pydantic-settings' default JSON parsing for list-shaped env vars (the project has no comma-separated list precedent).
- [x] Add a `field_validator` (or `model_validator`) on `auth_mode` that raises a typed `ConfigurationError` if the value is `"token"`, `"proxy_headers"`, or `"oidc"` with message `"auth mode '<value>' is reserved for a future build but not implemented at this cutover"`.
- [x] Unit test `tests/aizk/conversion/test_settings.py`: `Settings(auth_mode="trust_network")` constructs successfully.
  Added at `tests/conversion/unit/utilities/test_config.py` (existing settings test location).
- [x] Unit test: `Settings(auth_mode="token")` raises `ValidationError` with the not-implemented message.
  Adjusted to assert `ConfigurationError`: pydantic v2 only wraps `ValueError`/`AssertionError`; the task-specified typed error propagates directly.
  Parametrised across `token`, `proxy_headers`, `oidc`.
- [x] Unit test: `Settings(auth_mode="lol_bypass")` raises `ValidationError` (rejected by the literal type before the validator runs).
- [x] Unit test: `Settings()` with no `AIZK_AUTH_MODE` env var defaults to `"trust_network"` (confirm this is the desired default vs. requiring an explicit value).
- [x] Unit test: `Settings()` parses `AIZK_TRUSTED_HOSTS="api.example.com,*.internal"` into `["api.example.com", "*.internal"]` (or whatever the project's list parsing does — verify against existing field).
  Test asserts JSON-list parsing (`'["api.example.com", "*.internal"]'`) — pydantic-settings default; matches project convention.

## Principal model

- [x] Create `src/aizk/conversion/auth/__init__.py` (empty or re-exporting `Principal`).
- [x] Create `src/aizk/conversion/auth/principal.py` defining a frozen pydantic `BaseModel` `Principal` with fields `subject: str` and `provenance: Literal["trust_network"]`.
  Configure `model_config = ConfigDict(frozen=True)`.
- [x] Unit test `tests/aizk/conversion/auth/test_principal.py`: `Principal(subject="alice", provenance="trust_network")` constructs and round-trips through `model_dump_json()` / `model_validate_json()`.
- [x] Unit test: mutation attempt `principal.subject = "mallory"` raises a frozen-instance error.
- [x] Unit test: `Principal(subject="alice", provenance="token")` fails type-check at import time AND raises `ValidationError` at runtime (current `Literal` admits only `"trust_network"`).

## Trusted-host middleware

- [ ] Locate `create_app()` in `src/aizk/conversion/api/main.py`.
- [ ] In `create_app()`, BEFORE routers are mounted, call `app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)`.
  Import `TrustedHostMiddleware` from `starlette.middleware.trustedhost`.
- [ ] Add a one-line code comment at the middleware registration site noting that any future CORS middleware MUST be registered BEFORE this one (so CORS preflight succeeds even on Host-mismatch requests).
- [ ] Integration test `tests/aizk/conversion/api/test_trusted_host.py`: with `AIZK_TRUSTED_HOSTS=["api.example.internal"]`, a request to any endpoint with `Host: api.example.internal` succeeds.
- [ ] Integration test: a request with `Host: evil.example.com` returns HTTP 400 with body `"Invalid host header"` (the Starlette default).
- [ ] Integration test: a request with `Host: localhost` against default settings (`AIZK_TRUSTED_HOSTS` unset) succeeds.
- [ ] Integration test: a request with `Host: api.example.internal` AND `X-Forwarded-Host: api.example.internal` AND actual middleware-allowed-hosts `["other.example.internal"]` returns HTTP 400 — confirms `X-Forwarded-Host` is NOT consulted.
- [ ] Integration test: with `AIZK_TRUSTED_HOSTS=["*.internal"]`, a request with `Host: api.internal` succeeds (wildcard subdomain support).

## Principal resolution dependency

- [ ] Create or extend `src/aizk/conversion/api/dependencies.py` with `get_principal(request: Request, settings: Settings = Depends(get_settings)) -> Principal`.
  Implementation: `match settings.auth_mode: case "trust_network": return Principal(subject=settings.default_principal, provenance="trust_network"); case _: raise NotImplementedError(f"auth mode {settings.auth_mode!r} has no resolver branch")`.
- [ ] Unit test `tests/aizk/conversion/api/test_dependencies.py`: `get_principal` with `auth_mode="trust_network"` and `default_principal="local"` returns `Principal(subject="local", provenance="trust_network")`.
- [ ] Unit test: `get_principal` does NOT consult `request.headers["Authorization"]` or `request.headers["X-Forwarded-User"]` — set those headers, assert the returned subject is still `default_principal`.
- [ ] Unit test: when `settings.auth_mode` is forced to a value the resolver doesn't handle (bypass settings validator for the test), `get_principal` raises `NotImplementedError` (proves the safety net works).

## Datamodel additions

- [ ] Add `owner_id: str = Field(nullable=False, index=True)` to the `Source` SQLModel definition in `src/aizk/conversion/datamodel/source.py`.
- [ ] Add `owner_id: str = Field(nullable=False, index=True)` to the `Job` SQLModel definition in `src/aizk/conversion/datamodel/job.py`.
- [ ] Add `owner_id: str = Field(nullable=False, index=True)` to the `Output` SQLModel definition in `src/aizk/conversion/datamodel/output.py`.
- [ ] Verify the index naming convention against existing indexes on these tables (e.g., `ix_<table>_<column>`); confirm SQLModel auto-generates names matching the project's `op.create_index` calls in existing migrations.

## Alembic migration

- [ ] Create a new migration script in `src/aizk/conversion/migrations/versions/` named for the change (e.g., `<rev>_add_owner_id_columns.py`).
  Use the existing migration script style and `down_revision` pointing at the current head.
- [ ] In `upgrade()`: import `Settings` from the conversion settings module and read `default_principal_value = Settings().default_principal` once at the top of the function.
- [ ] In `upgrade()`: call `op.batch_alter_table("sources")` adding `owner_id TEXT` NULLABLE; same for `conversion_jobs` and `conversion_outputs`.
  Use `batch_alter_table` because SQLite requires the table-rebuild dance.
- [ ] In `upgrade()`: execute three `UPDATE <table> SET owner_id = :default WHERE owner_id IS NULL` statements bound to `default_principal_value`.
- [ ] In `upgrade()`: for each of the three tables, run `SELECT COUNT(*) FROM <table> WHERE owner_id IS NULL`; if any return > 0, raise `IrreversibleMigrationError` with a message identifying the per-table NULL counts.
- [ ] In `upgrade()`: `batch_alter_table` to alter `owner_id` to NOT NULL on all three tables.
- [ ] In `upgrade()`: `op.create_index("ix_sources_owner_id", "sources", ["owner_id"])`; same pattern for `conversion_jobs` and `conversion_outputs`.
- [ ] In `downgrade()`: `op.drop_index("ix_sources_owner_id")`; same for the other two indexes; then `batch_alter_table` to drop `owner_id` from each table.
- [ ] Migration test `tests/conversion/unit/test_migrations.py`: upgrade-to-head on a database pre-populated with rows in all three tables → every row has `owner_id = AIZK_DEFAULT_PRINCIPAL`, all three columns are NOT NULL, all three indexes exist.
- [ ] Migration test: full round-trip (upgrade → downgrade → upgrade) leaves the database in the post-upgrade shape with row data intact.
- [ ] Migration test: ORM-baseline equivalence test now passes for the three new columns (the existing equivalence-check fixture should pick this up automatically).
- [ ] Migration test (contrived NULL injection): use a connection-level trigger or direct SQL to insert a NULL `owner_id` after the backfill but before the NOT NULL assertion → migration aborts with `IrreversibleMigrationError` and identifies the offending table.

## API materialization

- [ ] Locate the Source / Job materialization helper(s) in `src/aizk/conversion/api/routers/jobs.py` (or wherever `_materialize_source` / `_create_job` live).
- [ ] Update the Source-creation path to accept a `Principal` argument and set `owner_id = principal.subject` on the new `Source` row.
- [ ] Update the Job-creation path to accept a `Principal` argument and set `owner_id = principal.subject` on the new `Job` row.
- [ ] On the `INSERT ... ON CONFLICT (source_ref_hash) DO NOTHING` path, confirm by inspection that the existing Source row's `owner_id` is preserved (no `DO UPDATE` clause).
  Add a code comment if necessary.
- [ ] Update the `POST /v1/jobs` route handler to declare `principal: Principal = Depends(get_principal)` and pass it into the materialization helpers.
- [ ] Audit other write-path routes (retry, cancel, bulk-actions) and add `principal: Principal = Depends(get_principal)` parameters so the dependency runs uniformly.
  Per the proposal's resolved Open Questions, do NOT persist `last_actor_id` at this cutover.
- [ ] Integration test: submit a job with `AIZK_DEFAULT_PRINCIPAL=alice` → the new `sources` row has `owner_id="alice"` and the new `conversion_jobs` row has `owner_id="alice"`.
- [ ] Integration test: submit two jobs whose `source_ref` canonicalizes to the same hash, with two different `default_principal` values across the two requests (mock settings per-request) → exactly one `sources` row exists, its `owner_id` matches the first submitter, both `conversion_jobs` rows have their respective request's `owner_id`.
- [ ] Integration test: the OpenAPI schema for `POST /v1/jobs` does NOT include `owner_id` in any request or response field — confirms internal-only invariant.

## Worker materialization

- [ ] Locate the Output-creation path in the conversion worker (search for the existing Output insert; likely in `src/aizk/conversion/worker/` or wherever the spec's "Create a conversion output record on success" requirement is implemented).
- [ ] Modify the Output insert to read `owner_id` from the parent `Job` row in the same database session and copy it onto the new `Output` row.
- [ ] If `Job.owner_id` is somehow NULL at Output-creation time, raise a typed error (e.g., `MissingOwnerOnJob`) and fail the job non-retryably.
  Note that this should be impossible after the migration; the check is defensive.
- [ ] Confirm the worker NEVER writes to `Job.owner_id` in any code path; grep for `Job.owner_id =` assignments after the change and assert there are none outside the API materialization path.
- [ ] Integration test: a Job with `owner_id="bob"` runs to success → the new `conversion_outputs` row has `owner_id="bob"`.
- [ ] Integration test: a Job whose `owner_id` is somehow NULL at runtime (force-injected via direct SQL) → the worker fails the job non-retryably with `MissingOwnerOnJob`; the Output row is NOT created.

## Documentation

- [ ] Add a new section to the deployment / operator documentation (likely `docs/deployment.md` or the project README) describing `AIZK_AUTH_MODE`, `AIZK_DEFAULT_PRINCIPAL`, and `AIZK_TRUSTED_HOSTS` settings and their defaults.
- [ ] In the deployment docs, explicitly call out that `AIZK_TRUSTED_HOSTS` MUST be overridden from the `["localhost", "127.0.0.1"]` default for any non-localhost deployment, and that the reverse proxy MUST rewrite `Host` to a value in the allowlist AND strip any client-supplied `X-Forwarded-Host`.
- [ ] In the deployment docs, document that `AIZK_AUTH_MODE` only supports `trust_network` at this build; reserved values `token`, `proxy_headers`, `oidc` will be implemented in a future change.
- [ ] Add a brief note in the developer-facing docs (e.g., a CONTRIBUTING section or the auth module's docstring) that adding a new auth mode is a delta on (1) the `Principal.provenance` literal, (2) the `Settings.auth_mode` literal validator, (3) the `get_principal` resolver match, and (4) test coverage — no migration required.

## End-to-end validation

- [ ] E2E test: full pipeline — submit a job in `trust_network` mode, worker processes it, output is uploaded → all three rows (`sources`, `conversion_jobs`, `conversion_outputs`) carry `owner_id = AIZK_DEFAULT_PRINCIPAL`.
- [ ] E2E test: API process refuses to start with `AIZK_AUTH_MODE=token` — the test launches the app with the bad env var and asserts a non-zero exit.
- [ ] E2E test: a fresh-clone deployment (default settings) accepts `Host: localhost` and `Host: 127.0.0.1` requests on `/health/live`.
- [ ] E2E test: with `AIZK_TRUSTED_HOSTS=["api.example.internal"]` and a request `Host: localhost`, response is HTTP 400 — confirms operator override removes the localhost default.
