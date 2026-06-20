# Design: Pluggable Database

## Context

The database layer lives under `aizk.conversion` — `aizk.conversion.db` (`get_engine`), `aizk.conversion.migrations` (the Alembic tree, which already holds both conversion and graph tables), and `database_url` + `litestream_*` on `ConversionConfig` — yet `aizk.graph` already depends on all of it.
SQLite + Litestream is the minimal-infrastructure default; Postgres is a near-future alternative backend (a scale-up option now, intended as a co-equal default by beta).
ADR-003 owns the database decision.
The single-writer SQLite assumption is preserved by this change; Postgres revisits it in its own change.

This change establishes a shared, stage-independent database foundation with a thin backend seam, implements the SQLite arm behind it, and rescopes the SQLite/Litestream contracts to the SQLite backend.
Only SQLite is built; Postgres is the named consumer that justifies the seam.

## Decisions

### Decision: A shared `aizk.db` module with a `backends/` seam

**Chosen:** A top-level `aizk.db` package owning engine creation, the migration runner, and database configuration, with backend-specific arms under `aizk.db.backends.<backend>`.
The SQLite arm (`aizk.db.backends.sqlite`) holds engine wiring and the Litestream durability manager.

**Rationale:** The database is shared foundation used by every stage, parallel to the existing top-level `aizk.pipeline`.
A `backends/` sub-namespace makes "SQLite is one of several" explicit for the incoming Postgres arm.

**Alternatives considered:**

- Keep it in `aizk.conversion` — rejected; it is exactly the cross-stage smell this change fixes.
- `aizk.replication` — rejected; too narrow (replication is one backend's concern) and implies a universal always-present service.
- `aizk.storage` — rejected; collides with S3/blob artifact storage, which the project already calls "storage."

### Decision: Backend identity is derived from the database URL, fail-closed

**Chosen:** `DatabaseConfig` exposes a backend identity computed from the `database_url` scheme.
SQLite is the only supported backend in this change; any other (unknown scheme, or `postgresql://` before its arm exists) raises a clear error at startup.

**Rationale:** Single source of truth (the URL already determines the engine), no second field that can drift out of sync.
Fail-closed is safer than today's silent inertness, which would let a half-configured non-SQLite deployment proceed.

**Alternatives considered:**

- A separate explicit `backend` config field — rejected; it can disagree with the URL and needs cross-validation.
- Recognize `postgresql://` and proceed inertly — rejected; the Postgres arm does not exist yet, so proceeding would half-run on an unsupported backend.

### Decision: Litestream stays embedded and role-gated; no standalone sidecar

**Chosen:** Keep the existing embedded, role-gated Litestream mode (one process matching `litestream_start_role` replicates).
No standalone replication service in this change.

**Rationale:** Postgres is the scale path; the standalone-replicator's main benefit (decoupling the singleton replicator from scaled app processes) is exactly the scenario where Postgres is the better answer.
Keeping SQLite replication simple avoids investing in a topology that overlaps with "switch to Postgres."

**Alternatives considered:** A standalone sidecar entrypoint — deferred; revisit only if a measured need appears that Postgres does not address.

### Decision: Move the Alembic tree to `aizk.db.migrations`

**Chosen:** Move the migration package (runner, `env.py`, `versions/`) into `aizk.db.migrations`. `env.py` imports every stage's models so `SQLModel.metadata` registers the full schema for autogenerate and the equivalence check.

**Rationale:** Leaving migrations under conversion perpetuates the smell.
The `schema-migrations` contract is location-agnostic (the runner resolves its script location from its own package), so the move is behavior-preserving.

**Risk/mitigation:** `alembic.ini` `script_location` and any tooling/CI referencing the path must be updated; the schema-equivalence and upgrade/downgrade round-trip tests are the guardrail that the move changed nothing observable.

### Decision: Config boundaries — DB config moves; shared S3 config does not

**Chosen:** `database_url` and the `litestream_*` fields move off `ConversionConfig`: `database_url` (and the derived backend identity) into `DatabaseConfig`; the `litestream_*` fields into the SQLite backend's durability config.
The **shared object-storage settings** (`s3_endpoint_url`, `s3_bucket_name`, `s3_region`, keys) are **not** moved — they serve conversion artifacts and graph markdown too, so they are object-storage foundation, not DB-specific.
The SQLite durability config reads the S3 settings it needs for the replica config from the **same environment variables** via its own settings model, rather than importing `ConversionConfig`.

**Rationale:** Decouples the DB layer from conversion at the code level (no cross-stage import) while leaving a genuinely shared concern (S3) where it is, keeping this change scoped.
Env var names are unchanged (`AIZK_DATABASE_URL`, `AIZK_LITESTREAM_*`, `AIZK_S3_*`), so no config migration/deprecation is required.

**Alternatives considered:**

- Move S3 settings into `aizk.db` — rejected; S3 is not DB-specific (artifacts, markdown), so it would mis-home a shared concern.
- Introduce an object-storage foundation config now — rejected as scope creep; noted as a future cleanup.
  The cost accepted here is that the S3 field definitions are read by two settings models (same env), to be deduped when that foundation lands.

### Decision: ADR-003 extension

**Chosen:** Extend ADR-003 (database) to record the backend seam, deterministic backend selection, durability-per-backend (Litestream for SQLite; engine-managed for Postgres), and the SQLite→Postgres migration path — with only SQLite implemented now.

**Rationale:** Governance requires an ADR for this architecture/contract-boundary change; it documents how invariants are preserved per backend so the Postgres arm cannot silently weaken them.

## Architecture

```text
aizk/db/                       shared database foundation (no stage owns it)
  config.py      DatabaseConfig    database_url → backend identity; fail-closed
  engine.py      get_engine        (moved from aizk.conversion.db)
  migrations/    Alembic tree       (moved from aizk.conversion.migrations;
                                     env.py registers every stage's models)
  backends/
    sqlite/      engine wiring + durability (LitestreamManager, role-gated)
                                     (moved from aizk.conversion.utilities.litestream)
    postgres/    — future change —

dependency direction (downward only):
  aizk.conversion ─┐
  aizk.graph ──────┼──▶  aizk.db   (engine, migrations, config)
  future stages ───┘

shared, not moved:  AIZK_S3_* object-storage settings (artifacts, markdown, litestream replicas)
```

## Risks

- **Broad import churn.**
  Every importer of `aizk.conversion.db` / `aizk.conversion.migrations` / `ConversionConfig.database_url` / the litestream module moves (conversion, graph, tests).
  Mechanical; mitigated by running the full suite green before and after, and `rg`-sweeping the whole tree (incl. notebooks/scripts/docs) for the old paths.
- **Alembic move.** `script_location` / tooling / CI path references; mitigated by the existing schema-equivalence and round-trip migration tests.
- **S3 field duplication.**
  Two settings models read the same `AIZK_S3_*` env; accepted here to keep scope contained.
  The owner-endorsed end-state is a shared **object-storage foundation** owning S3 config for artifacts, markdown, and Litestream replicas alike — a follow-up change that deduplicates these readers.
- **Single-writer assumption.**
  Unchanged and explicitly preserved; Postgres revisits it in its own change.
