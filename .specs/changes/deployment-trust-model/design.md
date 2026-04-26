# Design: deployment-trust-model

## Context

The conversion API currently has no notion of "who" submitted a job — every request is implicitly trusted, and Source / Job / Output rows are created without a creator attribution.
The audit confirmed this is acceptable for the current container-on-internal-network deployment posture, but the project is moving toward published self-hosted containers fronted by reverse proxies and ingress gateways.

Constraints shaping the design:

- **SQLite is the database.**
  No native UUID type, no Row-Level Security (RLS).
  The owner_id column is plain TEXT, and any future per-owner enforcement will run at the application layer, not at the database layer.
- **Pydantic v2 + SQLModel** are the existing typing and ORM layers.
  Principal modeling slots into pydantic's discriminated-union pattern; column additions slot into SQLModel field definitions and Alembic migrations.
- **Single-process FastAPI** with a separate worker pool.
  The API resolves the Principal on every inbound request; the worker has no inbound request and therefore reads `owner_id` from the parent Job, never re-authenticating.
- **Existing migration precedent** for "add column NULLABLE → backfill → ALTER NOT NULL" is set by the `source_ref` / `source_ref_hash` NOT NULL migration; the new migration follows the same shape.
- **Existing typed-error precedent** for migrations is `IrreversibleMigrationError`; the owner_id migration reuses it for the abort-on-null pre-alter assertion.

## Decisions

### Decision: Principal model shape

**Chosen:** A pydantic `BaseModel` in `src/aizk/conversion/auth/principal.py`:

```python
class Principal(BaseModel):
    subject: str
    provenance: Literal["trust_network"]
    model_config = ConfigDict(frozen=True)
```

`provenance` is a `Literal` type so that future modes (`token`, `proxy_headers`, `oidc`) widen by extending the literal — exhaustive `match` statements in the resolver will fail to type-check until they handle the new variant.
`frozen=True` prevents accidental mutation after resolution.

**Rationale:** Discriminated-union shape is the standard pydantic pattern (matches `SourceRef` precedent), and exhaustive matching enforces future-proofing at the type level rather than relying on runtime checks.
The model is intentionally small — `subject` is the only durable attribute.
Provenance is not persisted to the database (it varies across requests for the same owner once multi-mode is supported); only `subject` reaches the `owner_id` column.

**Alternatives considered:**

- A bare `str` typedef: rejected because it loses the provenance discriminator; future modes would need to thread separate context.
- A `dataclass`: rejected because pydantic gives validation, JSON round-trip, and frozen-by-default for one decorator's worth of cost.
- Storing `provenance` in the database alongside `owner_id`: rejected because provenance is request-scoped, not row-scoped; a Source created in `trust_network` mode and later observed in a hypothetical `oidc`-enabled deployment is not magically OIDC-owned.

### Decision: Auth mode validation site

**Chosen:** Validate `AIZK_AUTH_MODE` inside the pydantic settings model itself (`field_validator` on the auth-mode field), so that misconfiguration is caught when settings are constructed during process startup — before the ASGI application is built.

The settings model defines the full set of recognized modes as a `Literal` (currently `"trust_network"`, with `"token"` / `"proxy_headers"` / `"oidc"` reserved as future-mode strings rejected at validation time with a "not implemented at this build" message).

**Rationale:** Failing in `Settings()` construction means the failure is observable on every startup path — pytest fixtures, ad-hoc scripts, the FastAPI lifespan, and the worker — without each of those paths re-implementing the check.
A typed pydantic error is the existing precedent for configuration validation in this project (`AIZK_CONVERTER__DOCLING__*` validators).

The "not yet implemented" rejection is a deliberate split from the simpler "value not in literal" rejection: literal values reserve names for future modes (so the type-checker can be exhaustive across the codebase) without admitting them as runtime-legal until their resolver branch lands.

**Alternatives considered:**

- Validation inside FastAPI's `lifespan()` startup: rejected; the worker doesn't run lifespan and would silently accept misconfiguration.
- A standalone validator function called at every entrypoint: rejected; duplicates surface area, easy to forget at a new entrypoint.

### Decision: Trusted-host enforcement mechanism

**Chosen:** Use Starlette's `TrustedHostMiddleware` registered before all route handlers, configured from `AIZK_TRUSTED_HOSTS`.
Default value `["localhost", "127.0.0.1"]` shipped in the settings model.
The middleware is added in `create_app()` before the routers are mounted, so the rejection happens before route logic runs.

`Forwarded` and `X-Forwarded-Host` are NOT honored as input to the trusted-host check.
The middleware checks the actual `Host` header that reached the API process; reverse-proxy deployments are responsible for ensuring the proxy sends a fixed, trusted `Host` header (rewriting it on the way in) AND for stripping any client-supplied `X-Forwarded-Host` so it cannot reach the API process.

The middleware returns **HTTP 400 with body `"Invalid host header"`** on a Host-allowlist miss — Starlette's default behavior.
The spec aligns to this default rather than customizing to HTTP 421 (which would be more semantically accurate as "Misdirected Request" but adds a custom-middleware layer for no security benefit).

Wildcard subdomain support is native to `TrustedHostMiddleware` — operators can configure `AIZK_TRUSTED_HOSTS=["*.internal.example.com"]` for multi-host deployments without re-implementing matching.

**Middleware ordering note:** if a CORS middleware is added later, it MUST be registered BEFORE `TrustedHostMiddleware` in `create_app()` so that CORS preflight (`OPTIONS`) requests can complete even when the Host header is missing or unusual; security middleware then runs on the actual request.
This is the standard FastAPI / Starlette ordering convention.

**Rationale:** Starlette's middleware is battle-tested, ships an explicit Host allowlist, and supports wildcards.
Honoring `X-Forwarded-Host` would re-open the DNS-rebinding-style attack the middleware is supposed to defend against — the attacker controls the upstream proxy's view.
The "proxy must rewrite Host AND strip forwarded variants" rule pushes the trust boundary to the proxy, where it belongs in this deployment model.
HTTP 400 vs. 421 is a semantic preference that doesn't change the security property; staying with the Starlette default avoids unnecessary middleware customization.

**Alternatives considered:**

- Custom middleware that consults `Forwarded` headers: rejected per the security argument above.
- Per-route enforcement via dependencies: rejected; defense-in-depth wants the rejection to be unconditional and pre-route.

### Decision: Principal resolution dependency wiring

**Chosen:** A FastAPI dependency `get_principal(request: Request, settings: Settings = Depends(get_settings)) -> Principal` registered in `src/aizk/conversion/api/dependencies.py`.
In `trust_network` mode the body is a single `match` on `settings.auth_mode`:

```python
match settings.auth_mode:
    case "trust_network":
        return Principal(subject=settings.default_principal, provenance="trust_network")
    case _:
        raise NotImplementedError(...)  # unreachable: settings validator rejects others at startup
```

Routes that materialize Source / Job rows declare `principal: Principal = Depends(get_principal)` as a parameter and pass `principal.subject` into the materialization helper.

**Rationale:** A single resolver function with an exhaustive `match` is the contract surface that future mode-additions extend.
The settings-validator-rejects-others-at-startup invariant means the `_` arm should be unreachable in practice; raising `NotImplementedError` rather than silently default-returning catches the case where someone adds a literal value but forgets to add the resolver branch.

**Alternatives considered:**

- Resolver that hides mode-specific logic per provenance type with a registry: rejected as premature; the registry is one mode at cutover.
- Direct construction inline in each route: rejected; spreads the resolution logic across N call sites.

### Decision: owner_id propagation in worker

**Chosen:** When the worker creates a `conversion_outputs` row, it reads `Job.owner_id` from the same database session and copies the value onto the new Output row.
The read-and-copy occurs inside the existing transaction that finalizes job status to SUCCEEDED, so the Output insert and the Job status update are atomic.

The worker SHALL NOT call any Principal resolver, SHALL NOT read environment variables for default-principal substitution, and SHALL NOT mutate `Job.owner_id`.
If `Job.owner_id` is somehow NULL at Output-creation time (impossible after the migration, but defensible-coding-wise), the worker raises a typed error and fails the job non-retryably; this guarantees that an Output row is never written without an owner.

**Rationale:** Owner attribution is established at the API trust boundary; the worker is downstream of that boundary and has no business inventing or substituting owner values.
Copy-from-parent-Job is the simplest correct propagation; any other mechanism (re-resolve in worker, fall back to default-principal) would create paths where Output ownership diverges from Job ownership, which would be a quiet correctness bug.

**Alternatives considered:**

- Worker resolves a Principal from environment: rejected; introduces a second attribution path that can drift from the API's path.
- Database trigger that auto-copies: rejected; SQLite trigger semantics are surprising under concurrent writes, and the logic is tiny enough to keep in application code.

### Decision: Source reuse `owner_id` semantics

**Chosen:** The first-writer's Principal subject is captured as the Source row's `owner_id`.
Concurrent submissions of the same `source_ref` (the existing `INSERT ... ON CONFLICT (source_ref_hash) DO NOTHING` path) result in exactly one Source row whose `owner_id` is the winner of the insert race; the loser's Job row is created with the loser's `owner_id`, but the Source row's `owner_id` is unchanged.

**Rationale:** Source identity is keyed on `source_ref_hash` and is intentionally shared across submitters; ownership of the _Source_ row is therefore "who first told the system about this content," not "everyone who has ever submitted it."
Job ownership remains accurate to each submission.
This composes naturally with future read-side filtering: a multi-principal deployment can show each principal their own Jobs (and the Outputs of those Jobs) without changing Source semantics.

**Alternatives considered:**

- Source `owner_id` reset on every insert via `ON CONFLICT ... DO UPDATE SET owner_id = excluded.owner_id`: rejected; encourages last-writer-wins, which is wrong for ownership attribution.
- Multi-owner Source via a separate `source_owners` join table: rejected; out of scope at cutover (single-principal deployments don't need it), and adding the join later is non-destructive.

### Decision: Migration shape and backfill resolution

**Chosen:** A single Alembic migration with the five-step sequence specified in the schema-migrations delta:

1. Add `owner_id` columns NULLABLE to all three tables.
2. Read `AIZK_DEFAULT_PRINCIPAL` from the migration runtime environment via the existing settings entrypoint, freeze the value into a local variable, and execute three `UPDATE` statements (`UPDATE <table> SET owner_id = :value WHERE owner_id IS NULL`).
3. Pre-alter NULL assertion: `SELECT COUNT(*) FROM <table> WHERE owner_id IS NULL` for each table; if any returns > 0, raise `IrreversibleMigrationError` with the per-table counts.
4. `ALTER COLUMN ... NOT NULL` per table.
5. Create three single-column indexes (`ix_sources_owner_id`, `ix_conversion_jobs_owner_id`, `ix_conversion_outputs_owner_id`).

The `AIZK_DEFAULT_PRINCIPAL` value is read **once at migration start**, not re-read per-table; this prevents a value change mid-migration (e.g., env-var rotation) from producing inconsistent owner attribution across the three tables.

**Rationale:** Matches the existing `source_ref` NOT NULL precedent's three-phase shape (column add → backfill → constraint tighten) and reuses `IrreversibleMigrationError` for the abort path.
The per-table NULL assertion is what catches operator error before the schema becomes irreversible — same defensive posture as the precedent.
SQLite's `ALTER TABLE ... ALTER COLUMN` requires the table-rebuild dance via `op.batch_alter_table`; Alembic handles this transparently.

**Alternatives considered:**

- Three separate migrations (one per table): rejected; complicates downgrade ordering and the runtime-environment read.
- Server-side default for the column: rejected; `AIZK_DEFAULT_PRINCIPAL` is a deployment setting, not a database constant — encoding it as a SQL default would be surprising and would diverge from the SQLModel field definition (which has no default).
- Backfill via a one-shot ad-hoc script: rejected; the migration must be self-sufficient so that test fixtures and operator runs converge on the same shape.

### Decision: Settings additions

**Chosen:** Three new fields on the existing `Settings` (or `ApiSettings`) model:

```python
auth_mode: Literal["trust_network", "token", "proxy_headers", "oidc"] = "trust_network"
default_principal: str = "local"
trusted_hosts: list[str] = ["localhost", "127.0.0.1"]
```

The `auth_mode` literal includes future modes as reserved values rejected by the validator with a "not implemented at this build" message (per the auth-mode validation decision).
`default_principal="local"` is the shipped default; operators can override.
`trusted_hosts` ships with the localhost defaults documented in the proposal; the operator-deployment docs MUST call out the override requirement for production.

Environment-variable mapping follows the existing `AIZK_*` convention via pydantic-settings: `AIZK_AUTH_MODE`, `AIZK_DEFAULT_PRINCIPAL`, `AIZK_TRUSTED_HOSTS`.
List-shaped env vars use the existing project parsing convention (likely comma-separated; confirm in implementation against existing list-shaped settings like the Docling host list).

**Rationale:** Consolidates the three new knobs in the same settings layer the rest of the deployment uses, with the same validation and env-var conventions.
No new settings module or loading path.

**Alternatives considered:**

- A nested `AuthSettings` sub-model: rejected at this scope; three fields don't justify a sub-model, and a future change adding `token_secret_key` / `oidc_issuer` etc. can promote them to a sub-model at that point without breaking existing env-var names (pydantic-settings supports flat → nested via `env_nested_delimiter`).

## Architecture

```text
                    inbound HTTP request
                              │
                              ▼
                 ┌─────────────────────────┐
                 │ TrustedHostMiddleware   │
                 │ (Host vs                │ ── 421 if mismatch
                 │  AIZK_TRUSTED_HOSTS)    │
                 └─────────────────────────┘
                              │ Host accepted
                              ▼
                 ┌─────────────────────────┐
                 │ FastAPI route           │
                 │  e.g. POST /v1/jobs     │
                 └─────────────────────────┘
                              │
                              ▼
                 ┌─────────────────────────┐
                 │ get_principal           │ ── Settings (Literal["trust_network"])
                 │ (Depends)               │
                 │  match auth_mode:       │
                 │    trust_network →      │
                 │      Principal(subject= │
                 │       AIZK_DEFAULT_     │
                 │       PRINCIPAL)        │
                 └─────────────────────────┘
                              │ Principal
                              ▼
                 ┌─────────────────────────┐
                 │ Materialization helpers │
                 │ (sources, conversion_   │
                 │  jobs)                  │
                 │ owner_id =              │
                 │   principal.subject     │
                 └─────────────────────────┘
                              │ rows committed
                              ▼
                          DB writes
                  ┌────────────────────┐
                  │ sources.owner_id   │
                  │ conversion_jobs.   │
                  │   owner_id         │
                  └────────────────────┘
                              │
                              │ (later, async)
                              ▼
                 ┌─────────────────────────┐
                 │ Worker on success       │
                 │ reads Job.owner_id;     │
                 │ writes                  │
                 │ conversion_outputs.     │
                 │   owner_id              │
                 └─────────────────────────┘
```

Migration shape (one-shot, executed at deploy time before the application binds):

```text
   add owner_id NULLABLE   (3 tables)
            │
            ▼
   backfill from AIZK_DEFAULT_PRINCIPAL  (read once, applied to 3 tables)
            │
            ▼
   assert no NULL rows remain  (3 SELECT COUNTs)
            │  (raise IrreversibleMigrationError on any NULL)
            ▼
   ALTER COLUMN ... NOT NULL  (3 tables)
            │
            ▼
   CREATE INDEX ix_<table>_owner_id  (3 indexes)
```

## Risks

- **Default trusted-host allowlist permits localhost; operator forgets to override.**
  Production deployment behind a reverse proxy without `AIZK_TRUSTED_HOSTS` overridden means any caller that can reach the listener with `Host: localhost` bypasses the host check.
  Mitigation: the operator deployment docs MUST state that `AIZK_TRUSTED_HOSTS` is a required override for non-localhost deployments.
  A follow-up hardening is to log a `WARNING` at startup if `AIZK_TRUSTED_HOSTS` is at default AND the listener is bound to a non-loopback address; out of scope for this change but a candidate follow-up.

- **Reverse proxy fails to rewrite Host header, instead forwarding attacker-controlled Host.**
  Same outcome as the previous risk in a different mode.
  Mitigation: the trusted-host check uses the actual `Host` the API process sees; the proxy is responsible for rewriting (e.g., nginx `proxy_set_header Host api.example.internal`).
  Documented in the deployment guide.

- **`AIZK_DEFAULT_PRINCIPAL` env-var rotation between migration runs.**
  If an operator changes the default-principal value between two installs that share a database (shouldn't happen, but), the migration's read-once snapshot is what gets backfilled; subsequent rows use whatever is current at insert time.
  Mitigation: documented in the migration's docstring; operator-facing change rather than a code defense.

- **Future auth-mode addition forgets to update the resolver match.**
  Adding `"token"` to the `Literal` widens the type, but if the `get_principal` resolver isn't updated in the same change, the `_` arm raises `NotImplementedError` at the first request — observable in tests and at deploy time.
  Mitigation: the `_: raise NotImplementedError` arm is the safety net; type-checker exhaustiveness on `match settings.auth_mode` flags the omission at CI.

- **Source reuse race between two principals creates ambiguous attribution.**
  Two principals submit the same `source_ref` simultaneously; the Source row's `owner_id` is the winner of the insert race, which is non-deterministic.
  Mitigation: by design (per the Source reuse decision); the Source represents shared content, the Jobs are per-principal.
  Tests cover the race outcome.

- **Migration runtime cannot read `AIZK_DEFAULT_PRINCIPAL`.**
  The migration is invoked from the FastAPI lifespan (where settings are loaded), from worker startup, and from test fixtures.
  If a fixture path bypasses settings loading, the migration cannot resolve a backfill value.
  Mitigation: the migration script imports `Settings()` directly inside its `upgrade()` (matching the existing pattern in the migrations package); test fixtures already provide an `AIZK_DEFAULT_PRINCIPAL` env var or an override.
  Implementation must verify this against the existing test fixture conventions.

- **`Principal` model is loaded at import time before settings exist.**
  The model itself is settings-independent (no `@validator` references settings); the resolver dependency is what reads settings.
  Mitigation: design intentionally separates the two — `Principal` import has no settings dependency.

- **Frozen Principal blocks legitimate downstream patterns.**
  Some test fixtures may want to construct a `Principal` with a different subject for a single test.
  Mitigation: `frozen=True` allows construction (just not mutation); fixtures construct fresh Principals per test rather than mutating a shared one.

- **owner_id widening to UUID later would require migration.**
  If a future change adopts UUID as the canonical Principal subject shape, the existing TEXT column would still accept UUID-shaped strings; no migration needed.
  Mitigation: TEXT is forward-compatible with both string-shaped and UUID-shaped subjects.
  Risk is mostly notional.
