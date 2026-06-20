# Proposal: Pluggable Database

## Intent

The database layer is bound to one engine and lives in the wrong place.
SQLite is wired in throughout, its durability is SQLite-specific (Litestream restore + replicate), and the whole layer — engine creation, the Alembic migration tree, `database_url`, and the Litestream manager — lives under `aizk.conversion`, even though the _graph_ stage (and every future stage) depends on it.
Postgres is a near-future alternative backend: a scale-up option now, and intended to be a co-equal default once the project reaches beta.

This change makes the database backend a **choice**.
It lifts the database into its own shared foundation (`aizk.db`) with a thin **backend seam**, rehomes the existing SQLite engine and its Litestream durability behind that seam, and scopes the SQLite/Litestream-specific contracts to the SQLite backend.
Only the SQLite arm is built here; Postgres is a separate downstream change that adds the second arm.
Durability stops being a global concern and becomes a per-backend one: Litestream for SQLite, the engine itself for Postgres.

## User Stories

### Story: choose-database-backend

As an operator, I want to choose my database backend — SQLite for a minimal-infrastructure self-hosted setup, Postgres to scale — so that I can run the smallest footprint that fits my needs and grow later without re-architecting the application.

### Story: database-is-shared-foundation

As a developer, I want the database layer to be a stage-independent shared foundation, so that the graph stage and future stages depend on a common database module rather than reaching into the conversion stage for the engine, migrations, and configuration.

### Story: durability-matched-to-backend

As an operator, I want durability and recovery handled the right way for whichever backend I run — Litestream replication/restore under SQLite, the engine's own mechanisms under Postgres — so that an idea captured once survives re-ingestion and recovery regardless of backend.

## Scope

Capabilities in build-dependency order: the **`pluggable-database`** seam + shared module first (it defines where everything lives and how a backend is selected), then **`sqlite-replication`** (rescoped as the SQLite backend's durability arm behind that seam). `schema-migrations` is touched only by the rehome of the Alembic tree.

**In scope:**

- **`pluggable-database` capability (new).**

  - A shared `aizk.db` module owning engine creation, the Alembic migration tree, and database configuration (`database_url` plus an explicit backend selector), independent of any stage.
  - A backend seam (`aizk.db.backends.<backend>`) that names what a backend provides — engine wiring and a durability lifecycle — with the **SQLite backend** as the only implementation here.
  - Backend selection is a deterministic function of configuration (the `database_url` scheme, surfaced as an explicit backend identity), so a deployment's backend is unambiguous and inertness for non-matching backends is explicit, not incidental.
  - All current importers (conversion, graph) updated to the shared home; runtime behavior preserved.
    The `AIZK_DATABASE_URL` environment variable is unchanged.

- **`sqlite-replication` capability (rescoped, not new).**

  - The existing Litestream restore/replicate/config-emit behavior moves to the SQLite backend (`aizk.db.backends.sqlite`) and its contracts are scoped explicitly to "when the backend is SQLite".
    The single-replicator and restore-before-writers invariants are stated as SQLite-backend properties.
  - Litestream keeps its embedded, role-gated mode as the default; a standalone sidecar process remains an optional deployment topology, not a requirement of this change.

- **ADR** extending ADR-003 (database): the backend seam, durability-per-backend, backend selection, and the SQLite→Postgres migration path — recording how invariants are preserved in each backend's dialect, with only SQLite implemented now.

**Out of scope:**

- **The Postgres backend implementation** — its engine wiring, durability story, and the single-writer-assumption changes Postgres allows are a separate later change; this change only prepares the seam it slots into.
- **A first-class standalone Litestream service** — Postgres is the scale path, so SQLite replication stays deliberately simple (embedded role-gate default); a sidecar is optional and not built here.
- **Changing the single-writer SQLite assumption or write-concurrency model** — preserved; Postgres revisits it in its own change.
- **Litestream internals** — WAL handling, checkpoint cadence, S3 upload semantics stay delegated to the binary (per the existing `sqlite-replication` spec).

## Approach

> Mechanism sandbox — contracts live in the delta specs; chosen mechanisms formalize in `design.md`.

- **Thin seam, not a framework.**
  The backend seam names only what differs by engine — how an engine is created and how durability is run — and nothing more.
  No engine-neutral meta-framework; the SQLite arm is concrete and Postgres is a named, near-term consumer of the same seam (the justification for the abstraction).
- **Rehome by moving, with behavior preserved.**
  Move engine creation, the Alembic tree, `database_url`/Litestream config, and `litestream.py` from `aizk.conversion` into `aizk.db`; update every importer (including graph's reuse); keep `AIZK_DATABASE_URL` and observable behavior identical.
  This is a broad-but-mechanical refactor plus a config regrouping.
- **Backend selection from the URL scheme.** `sqlite://…` vs `postgresql://…` already determines eligibility today (Litestream skips non-file-SQLite URLs); surface that as an explicit backend identity on the database config so selection is a first-class, testable value rather than an implicit side effect.
- **Durability is per-backend.**
  The SQLite backend owns Litestream (restore + replicate, embedded role-gate).
  A Postgres backend would own nothing app-side (the engine handles it).
  The shared seam exposes a durability lifecycle that SQLite implements and Postgres will later implement as a no-op/engine-deferred arm.

## Open Questions

1. **Config namespace.**
   Keep `database_url` flat as `AIZK_DATABASE_URL`, but where do the Litestream settings (currently `AIZK_LITESTREAM_*` on `ConversionConfig`) live — a new `AIZK_DATABASE__*` group, an `AIZK_SQLITE__*` backend group, or unchanged keys read by the new `DatabaseConfig`?
2. **Migration-tree move.**
   Move the Alembic tree to `aizk.db.migrations` now (cleanest, but touches `alembic.ini` / script locations and any tooling that references the path), or leave it in place this change and only rehome engine/config/durability?
3. **Sidecar in or out.**
   Fully defer the optional standalone Litestream sidecar to a future change, or include a thin opt-in sidecar entrypoint here for the multi-process-single-node compose case?
4. **Backend selector shape.**
   Derive backend identity purely from the `database_url` scheme, or add an explicit `backend` config field that must agree with the URL (fail-closed on mismatch)?
