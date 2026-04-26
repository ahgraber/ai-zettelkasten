# Proposal: deployment-trust-model

## Intent

The conversion API runs without an app-layer authentication mechanism — every request that reaches the FastAPI process is implicitly trusted, and there is no notion of "who" submitted a job.
This is acceptable for the current container-on-internal-network deployment posture confirmed in the 2026-04-25 audit, but the project will be published for self-hosted deployment behind reverse proxies and ingress gateways.
Without an explicit trust model, the boundary between "the API trusts the network" and "the API trusts the caller" is ambient knowledge rather than a contract; and without recording who created each row, retrofitting multi-tenant isolation later would require a data-model migration on already-deployed installs.
This change makes the trust model explicit, formalizes a single supported deployment shape (`trust_network`) at cutover, and adds Principal recording to all created rows so future auth modes plug in without schema changes.

## Scope

**In scope:**

- New `Principal` abstraction with `subject` and `provenance`, resolved on every request via a FastAPI dependency.
- New `AIZK_AUTH_MODE` configuration setting accepting only `trust_network` at cutover.
- Startup-time refusal: the API process SHALL fail to start if `AIZK_AUTH_MODE` is unset, unrecognized, or set to a mode that exists in the type but is not implemented.
- New `AIZK_DEFAULT_PRINCIPAL` setting supplying the subject in `trust_network` mode.
- New `AIZK_TRUSTED_HOSTS` allowlist enforced on every request as defense-in-depth for `trust_network` deployments.
- Three new `owner_id` columns: `sources.owner_id`, `conversion_jobs.owner_id`, `conversion_outputs.owner_id` (NOT NULL after backfill).
- Alembic migration that adds the columns NULLABLE, backfills to `AIZK_DEFAULT_PRINCIPAL`, then alters to NOT NULL — matching the source_ref/source_ref_hash NOT NULL migration precedent.
- API materialization writes `owner_id` from the resolved Principal on every Source and Job insert.
- Worker copies `owner_id` from the parent Job onto each Output it creates.
- Non-normative spec note that Principal `provenance` is intentionally extensible to support future `token`, `proxy_headers`, and `oidc` modes; only `trust_network` is normative at cutover.

**Out of scope:**

- `token` auth mode (app-layer bearer tokens).
- `proxy_headers` auth mode (identity injected by upstream reverse proxy).
- OIDC / SAML / SSO integration.
- Per-principal read-side query filtering — `trust_network` is single-principal, so filtering would be a no-op today.
  Adding owner_id to API responses or scoping list queries by owner is a separate future change.
- Audit logging of principal-attributed mutations (retry, cancel, bulk actions).
- CSRF tokens for UI mutating routes.
- Session management or login UI.
- Network egress validation — covered separately by `network-egress-policy`.

## Approach

Mechanism notes parked here for later promotion to `design.md`:

- **Principal model.**
  New module `src/aizk/conversion/auth/principal.py` with a pydantic `Principal(BaseModel)` carrying `subject: str` and `provenance: Literal["trust_network"]`.
  The `Literal` widens in future changes; `trust_network` is the only legal value at cutover.
- **Resolution dependency.**
  FastAPI dependency `get_principal(request: Request) -> Principal` registered in `src/aizk/conversion/api/dependencies.py`.
  In `trust_network` mode it returns `Principal(subject=settings.default_principal, provenance="trust_network")` without inspecting the request body or headers (other than the trusted-host check).
- **Startup-time mode validation.**
  In `lifespan()` (or a startup validator), inspect `AIZK_AUTH_MODE`.
  Raise a typed startup error and exit non-zero if the value is unset, unrecognized, or names a mode that is not implemented (e.g., `token` at cutover).
  This guarantees that a misconfigured deployment fails loud, not silently default-open.
- **Trusted-host enforcement.**
  FastAPI middleware that runs before route handlers; reads `Host` header, compares against `AIZK_TRUSTED_HOSTS` (default `["localhost", "127.0.0.1"]`).
  Mismatch returns HTTP 421 Misdirected Request.
  This is FastAPI's `TrustedHostMiddleware`-equivalent behavior, configured to fail closed.
- **Schema additions.**
  Add `owner_id: str` columns to the SQLModel definitions for `Source`, `Job`, and `Output`.
  Add `Index("ix_sources_owner_id", "owner_id")` and parallel indexes on the other two tables — sized to support future per-principal filtering without an additional migration.
- **Migration shape.**
  Three-step migration matching the existing `source_ref` NOT NULL precedent:
  1. `op.add_column("sources", sa.Column("owner_id", sa.String(), nullable=True))` (and parallel for jobs/outputs).
  2. `op.execute("UPDATE sources SET owner_id = :default WHERE owner_id IS NULL", default=settings.default_principal)`; same for jobs and outputs.
  3. `op.alter_column("sources", "owner_id", nullable=False)` (and parallel).
     The migration's `downgrade()` drops the columns; reversibility is unconditional (no widening of the data model — every row gets a single principal).
- **API materialization.**
  Update Source and Job creation paths in `src/aizk/conversion/api/routers/jobs.py` (and any sibling materializers) to accept the resolved `Principal` and persist `owner_id = principal.subject` on insert.
- **Worker materialization.**
  The worker creates Output rows (per the conversion-worker spec, "Create a conversion output record on success"); on insert, the worker SHALL copy `owner_id` from the parent Job — there is no Principal in the worker's execution context.
  This keeps owner attribution intact across the API → DB → worker boundary without re-authenticating in the worker.
- **Extensibility note.**
  Principal `provenance` is a discriminated `Literal` so adding `token` / `proxy_headers` later is a delta on the type plus a new `get_principal` branch — no migration, no API surface change for existing routes.

## Schema Impact

OpenAPI surface SHALL remain unchanged at cutover.
Principal resolution is server-side only; `owner_id` is recorded internally and is not surfaced in any request or response shape.
Future API changes that add owner-aware list filtering would be a separate spec change with its own OpenAPI delta.

Database schema changes:

- `sources` gains a NOT NULL `owner_id` text column with an index.
- `conversion_jobs` gains a NOT NULL `owner_id` text column with an index.
- `conversion_outputs` gains a NOT NULL `owner_id` text column with an index.

A before-snapshot of the OpenAPI spec will be captured for verification; the expected-changes file will declare zero diff for the API surface and document the database schema additions.

## Open Questions

None remaining at proposal time.
Resolved during proposal review:

- **Trusted-host default:** ship `["localhost", "127.0.0.1"]` so a fresh clone runs out of the box.
  Operators deploying behind a reverse proxy SHALL override `AIZK_TRUSTED_HOSTS` to the public-facing hostname; the docs MUST call this out.
- **Principal-on-mutations:** deferred to a future audit-logging spec.
  Retry / cancel / bulk-action endpoints SHALL resolve the Principal (so the dependency runs uniformly) but SHALL NOT persist `last_actor_id` at this cutover.
  Adding the column later is a non-destructive ALTER.
- **`owner_id` column type:** `str` (TEXT in SQLite).
  Rationale: SQLite has no native UUID type so UUIDs are stored as TEXT regardless; future auth modes (`oidc`, `proxy_headers`) deliver subject identifiers from external sources (`sub` claims, proxy-injected user names) that are string-shaped, not UUID-shaped — locking `owner_id` to UUID would force a translation table for every non-`trust_network` mode.
  Existing precedent: `aizk_uuid` is internal-opaque (UUID-shaped TEXT), `karakeep_id` is external (plain TEXT); `owner_id` is conceptually the latter.
  The recommended multi-tenant indexing pattern (compound index with `owner_id` first) works regardless of TEXT vs UUID.
