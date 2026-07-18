# Proposal: shared-auth-config

## Intent

Authentication and configuration are shared-kernel concerns that currently live inside the `aizk.conversion` package.
Every other surface reaches into conversion for them: the graph operator API and the new operator console import `aizk.conversion.auth` (`Principal`), `aizk.conversion.utilities.config` (`ConversionConfig`, `AuthSettings`), and the `get_principal` FastAPI dependency from `aizk.conversion.api.dependencies` — none of which is conversion's to own.
This inverts the dependency graph: domain and presentation packages depend on a sibling *domain* for auth, config, and the trusted-host perimeter, so a change to conversion can ripple into graph and the console for reasons that have nothing to do with conversion.
The database kernel already escaped this trap — `pluggable-database` lifted it into a shared `aizk.db` behind a backend seam.
Auth and config are the remaining stragglers.
This change lifts the shared auth primitives and the deployment-shared configuration into top-level kernels so downstream packages depend on `aizk.auth` and `aizk.config`, not on `aizk.conversion`.

## User Stories

### Story: honest-dependency-graph

As the maintainer, I want domain and presentation packages to depend on shared kernels rather than on a sibling domain, so that the dependency graph is acyclic and a conversion change does not ripple into graph or the console for non-conversion reasons.

### Story: cheap-surface-onboarding

As the developer adding a pipeline stage or operator surface, I want auth, config, and the request perimeter available as shared primitives, so that a new surface wires to `aizk.auth`/`aizk.config` without importing the conversion package for things that are not conversion's.

Both ladder to the north star's self-hosting clause — "self-hostable on minimal infrastructure so one person can own the entire system": a system one person can own is one whose module boundaries are honest, so the shared foundation is not hidden inside one domain.

## Scope

**In scope:**

- A new `aizk.auth` kernel holding the framework-agnostic principal model and resolution: `Principal`, `AuthSettings`, and the resolver logic currently in `aizk.conversion.auth`, plus a thin FastAPI adapter exposing the `get_principal` dependency both apps already share.
- A shared `aizk.config` base holding the deployment-shared settings currently bundled in `ConversionConfig` — the database location, the trusted-host allowlist (the request perimeter), and the auth settings.
  Conversion-specific settings (queue depth, converter selection, S3/object-storage) stay in a conversion config that composes the shared base.
- Rewiring the importers — `aizk.conversion`, `aizk.graph.api`, `aizk.console` — to depend on the shared kernels.
- Preserving the `AIZK_<SECTION>__<FIELD>` env-var conventions and the OpenAPI-visible auth behavior exactly (pure relocation and dependency inversion, no behavior change).

**Out of scope:**

- Any auth-model change: still single-principal `trust_network`; multi-principal modes (`token`, `proxy_headers`, `oidc`) remain future work on the `Principal` type and resolver.
- New auth backends or perimeter mechanisms.
- Renaming or relocating the app entrypoints (`aizk.conversion.api.main`, `aizk.graph.api.main`) — a separate concern the operator-console change explicitly deferred.
- Moving the per-domain JSON APIs to a top-level `aizk.api` (package-by-layer): rejected in favor of the existing vertical-slice convention.
- Database backend work: owned by `pluggable-database`; this change follows its shared-kernel direction but does not touch the DB seam.

## Approach

Follow the `pluggable-database` precedent: move the shared code to its top-level kernel, keep env-var names stable so no settings migration is required, and update imports tree-wide.
Split `ConversionConfig` along its true seam — shared deployment settings into `aizk.config`, conversion-specific settings composed on top — rather than lifting the whole class.
Sequence relative to `pluggable-database` so the two shared-kernel efforts do not collide over config ownership.
A preliminary-research ADR in `docs/decision-record/` should record the kernel boundary (what is shared vs. domain-specific) before implementation, per the project's structural-choice policy.

## Schema Impact

- No JSON API schema change is intended: `get_principal` behavior and the OpenAPI surface are preserved.
- Configuration field env-var names are preserved; if any prove impossible to keep, the affected settings carry a migration note and deprecation window, per the project's config-change policy.
