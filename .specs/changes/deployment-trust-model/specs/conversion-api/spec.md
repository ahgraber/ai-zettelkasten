# Delta for conversion-api

## ADDED Requirements

### Requirement: Validate auth mode at API startup

The API process SHALL validate the `AIZK_AUTH_MODE` configuration value during startup and SHALL refuse to start if the value is unset, unrecognized, or names a mode that is not implemented in the current build.
At cutover, the only implemented mode is `trust_network`; any other value (including `token`, `proxy_headers`, `oidc`, an empty string, or any string outside the implemented set) SHALL cause the process to fail to start with a typed startup error and a non-zero exit code.

This requirement guarantees that a misconfigured deployment fails loudly at process boot rather than silently default-opening to an unintended trust posture.

#### Scenario: trust_network mode boots successfully

- **GIVEN** `AIZK_AUTH_MODE=trust_network` and `AIZK_DEFAULT_PRINCIPAL` is set
- **WHEN** the API process starts
- **THEN** the process completes startup, the `/health/live` endpoint returns 200, and request handling proceeds

#### Scenario: Unset auth mode rejected at startup

- **GIVEN** `AIZK_AUTH_MODE` is not set in the process environment
- **WHEN** the API process is launched
- **THEN** startup raises a typed configuration error and the process exits non-zero before binding the HTTP listener

#### Scenario: Unimplemented auth mode rejected at startup

- **GIVEN** `AIZK_AUTH_MODE=token` (a value reserved for a future change but not implemented at this cutover)
- **WHEN** the API process is launched
- **THEN** startup raises a typed configuration error identifying the mode as not-yet-implemented and the process exits non-zero before binding the HTTP listener

#### Scenario: Unknown auth mode rejected at startup

- **GIVEN** `AIZK_AUTH_MODE=lol_anyone_can_in`
- **WHEN** the API process is launched
- **THEN** startup raises a typed configuration error identifying the value as unrecognized and the process exits non-zero

### Requirement: Resolve a Principal on every API request

The API SHALL resolve a `Principal` value on every inbound request before route handlers execute, and SHALL make the resolved Principal available to handlers via dependency injection.
A `Principal` carries at minimum a `subject: str` identifier and a `provenance` discriminator that names the auth mode that produced it.
At cutover, the only legal `provenance` value is `"trust_network"`; the type SHALL be defined as a discriminated union so that future auth modes (`token`, `proxy_headers`, `oidc`) extend it without changing the routes that consume it.

In `trust_network` mode, the resolver SHALL return `Principal(subject=AIZK_DEFAULT_PRINCIPAL, provenance="trust_network")` for every request without inspecting the request body or auth-bearing headers.
The Principal value is the same for every request in this mode by design — `trust_network` is single-principal.

This requirement does not by itself create or persist anything; it establishes the contract that every request handler can see "who" the request belongs to without each handler inventing its own resolution path.

#### Scenario: Principal resolved on every request in trust_network mode

- **GIVEN** `AIZK_AUTH_MODE=trust_network` and `AIZK_DEFAULT_PRINCIPAL=local`
- **WHEN** any API request reaches a route handler
- **THEN** the handler receives a `Principal(subject="local", provenance="trust_network")` via the dependency, regardless of route or method

#### Scenario: Principal resolution does not consult auth headers in trust_network mode

- **GIVEN** the request carries `Authorization: Bearer <anything>` or `X-Forwarded-User: someone-else`
- **WHEN** the Principal dependency runs
- **THEN** the resolved `subject` equals `AIZK_DEFAULT_PRINCIPAL` and `provenance == "trust_network"`; the request headers do not influence the resolution

### Requirement: Enforce trusted-host allowlist on every request

Every inbound request SHALL be checked against the `AIZK_TRUSTED_HOSTS` allowlist before route handling proceeds.
A request whose `Host` header does not match a value in the allowlist SHALL be rejected with HTTP 400 (the Starlette `TrustedHostMiddleware` default), and the route handler SHALL NOT execute.
The check SHALL operate on the actual `Host` header that reaches the API process.
`Forwarded` and `X-Forwarded-Host` SHALL NOT be honored as input to this check; reverse-proxy deployments are responsible for rewriting `Host` to a trusted value before forwarding (and for stripping any client-supplied `X-Forwarded-Host`).

The shipped default for `AIZK_TRUSTED_HOSTS` is `["localhost", "127.0.0.1"]` so that a fresh clone runs out of the box.
Operators deploying behind a reverse proxy or ingress gateway SHALL override this setting to the public-facing hostname; the configuration documentation SHALL call this out as a deployment requirement.

This requirement is defense-in-depth for the `trust_network` posture: the network is presumed trusted, but a misconfigured ingress that forwards arbitrary `Host` headers should not allow DNS-rebinding-style attacks against the API.

#### Scenario: Request to allowed host accepted

- **GIVEN** `AIZK_TRUSTED_HOSTS=["api.example.internal"]` and a request with `Host: api.example.internal`
- **WHEN** the request reaches the API
- **THEN** the trusted-host check passes and the route handler executes normally

#### Scenario: Request to disallowed host rejected

- **GIVEN** `AIZK_TRUSTED_HOSTS=["api.example.internal"]` and a request with `Host: evil.example.com`
- **WHEN** the request reaches the API
- **THEN** the API returns HTTP 400 and the route handler does not execute

#### Scenario: Default allowlist permits localhost

- **GIVEN** `AIZK_TRUSTED_HOSTS` is not set (default applies) and a request with `Host: localhost`
- **WHEN** the request reaches the API
- **THEN** the trusted-host check passes

### Requirement: Principal abstraction is intentionally extensible (non-normative)

The `Principal` type and `get_principal` dependency are designed so that future auth modes (`token`, `proxy_headers`, `oidc`) plug in without changing route handlers, materialization helpers, or the database schema introduced by this change.
This requirement records the design intent so that a future spec change adding one of those modes is a delta on the `Principal` type and the resolver, not a refactor of every route.

This requirement is non-normative — it has no scenarios.
The normative requirements above (`Validate auth mode at API startup`, `Resolve a Principal on every API request`) define the observable behavior; this requirement is a marker that the future widening is anticipated.

## MODIFIED Requirements

### Requirement: Accept job submission without external service calls

The existing requirement is extended with one additional materialization clause:

The API SHALL also persist `owner_id = principal.subject` on every Source row created or reused at submit time and on every Job row created.
The Principal is the value resolved by the dependency described in "Resolve a Principal on every API request".
For Source reuse under concurrent submission (the existing `INSERT ... ON CONFLICT (source_ref_hash) DO NOTHING` path), the `owner_id` of the winning insert SHALL be the principal of the request that won the race; subsequent submissions for the same `source_ref_hash` SHALL NOT overwrite the existing Source row's `owner_id`.
This means: at cutover, with single-principal `trust_network`, every Source row's `owner_id` is `AIZK_DEFAULT_PRINCIPAL`; in a future multi-principal deployment, the first submitter "owns" the Source and subsequent submitters can still create their own Jobs against it but do not change Source ownership.

(Previously: rows were created without an `owner_id` column.
The change adds the column and ties it to the resolved Principal.
The existing schema-reference clause is unchanged; the column is internal-only and does not appear in the request or response schemas.)

#### Scenario: owner_id recorded on Source and Job at submit

- **GIVEN** `AIZK_AUTH_MODE=trust_network`, `AIZK_DEFAULT_PRINCIPAL=local`, and a valid `KarakeepBookmarkRef` submission
- **WHEN** the API materializes Source identity and creates the Job
- **THEN** the new `sources` row has `owner_id = "local"` and the new `conversion_jobs` row has `owner_id = "local"`

#### Scenario: Source reuse preserves original owner_id

- **GIVEN** an existing `sources` row with `owner_id = "local"` and `source_ref_hash = H`, and a new submission whose `source_ref` canonicalizes to the same hash
- **WHEN** the API materializes Source identity (the `INSERT ... ON CONFLICT DO NOTHING` resolves to reuse)
- **THEN** the existing Source row's `owner_id` is unchanged; only the new Job row is created, with its own `owner_id` from the current Principal

## Technical Notes

- The Principal abstraction lives at `src/aizk/conversion/auth/principal.py` (new module).
- The Principal-resolution dependency is registered in `src/aizk/conversion/api/dependencies.py` and consumed by route handlers via `Depends(get_principal)`.
- Trusted-host enforcement is wired as ASGI middleware in `src/aizk/conversion/api/main.py`, before route registration so that 421 rejections occur before any route logic.
