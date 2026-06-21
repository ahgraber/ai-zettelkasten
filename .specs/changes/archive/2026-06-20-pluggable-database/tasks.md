# Tasks: Pluggable Database

> Ordered by build dependency: the shared `aizk.db` foundation + backend selection first (everything else moves into it), then the Alembic tree, then the SQLite durability arm, then the tree-wide importer sweep and the ADR. This is a broad-but-mechanical rehome; the guardrail is that the **full suite is green before and after** and that an `rg` sweep of the whole tree (incl. notebooks/scripts/docs) finds no surviving references to the old paths. Run tests with `uv run pytest tests/`; delegate to the user only on a real sandbox/permission error (`uv sync`, `.env`).

## Shared database foundation and backend selection (pluggable-database)

- [x] Scaffold the `aizk.db` package: move `get_engine` from `aizk.conversion.db` into `aizk.db.engine`, preserving its URL-keyed engine caching behavior.
- [x] Add `aizk.db.config.DatabaseConfig` (pydantic-settings, `env_prefix="AIZK_"`) owning `database_url` (reading the unchanged `AIZK_DATABASE_URL`) and exposing a derived, read-only **backend identity** computed from the URL scheme; SQLite is the only supported backend, and an unsupported backend raises a clear, backend-naming error at resolution.
- [x] Test: a `sqlite://…` URL resolves to the SQLite backend identity and the engine is usable. (R2 — supported-backend selection)
- [x] Test: an unsupported backend URL (`postgresql://…` and an unknown scheme) fails closed at config resolution with an error naming the unsupported backend. (R2 — fail-closed; both partitions: not-yet-implemented and unknown)
- [x] Test: `aizk.graph` resolves the engine, migration runner, and database config from `aizk.db`, importing no `aizk.conversion` database module. (R1 — stage-independent foundation; structural assertion over graph's imports)

## Migration tree rehome (schema-migrations implementation; supports pluggable-database R1)

- [x] Move the Alembic package (`run_migrations`, `env.py`, `versions/`) from `aizk.conversion.migrations` to `aizk.db.migrations`; update `alembic.ini` `script_location` and any tooling/CI path references.
- [x] Ensure `env.py` imports every stage's models (conversion + graph) so `SQLModel.metadata` registers the full schema for autogenerate and equivalence.
- [x] Update all `run_migrations` invocation sites (API lifespan, worker/serve startup, test fixtures) to the new path.
- [x] Test: after upgrade-to-head through the moved tree, the schema is structurally equivalent to `SQLModel.metadata.create_all()` over every stage's models. (one migration tree governs all tables — R1; the migration suites were physically relocated to `tests/db/migrations/` mirroring `aizk.db.migrations`, and `tests/db/test_foundation.py` adds an explicit multi-stage-table assertion)
- [x] Test: the upgrade-to-head → downgrade-to-base round-trip still passes through the moved tree. (migration reversibility preserved; the per-stage round-trip suites relocated to `tests/db/migrations/`)

## SQLite durability arm behind the seam (sqlite-replication)

- [x] Move `aizk.conversion.utilities.litestream` to `aizk.db.backends.sqlite`, preserving the config-emission, path-safety, process-group-teardown, and restore-on-startup contracts unchanged.
- [x] Move the `litestream_*` settings into the SQLite backend's durability config; have it read the shared `AIZK_S3_*` object-storage settings from the same env via its own settings model (no import of `ConversionConfig`).
- [x] Rescope replication eligibility to consult the `DatabaseConfig` backend identity: replication starts only when the active backend is SQLite, replication is enabled, the role matches, and the database resolves to a file path.
- [x] Test: with the SQLite backend active, replication is disabled by config → manager inert, no subprocess, no raise. (eligibility — disabled partition)
- [x] Test: with the SQLite backend active and replication enabled but the role excluded (and not `both`) → manager inert. (eligibility — role partition)
- [x] Test: with the SQLite backend active but a file-less (in-memory) database → manager inert. (eligibility — file-less SQLite partition)

## Importer sweep, config cleanup, and ADR

- [x] Remove `database_url` and the `litestream_*` fields from `ConversionConfig`; repoint every reader (conversion, graph, tests) at `DatabaseConfig` / the SQLite durability config.
- [x] `rg`-sweep the whole tree (src, tests, notebooks, scripts, docs) for `aizk.conversion.db`, `aizk.conversion.migrations`, `aizk.conversion.utilities.litestream`, and `ConversionConfig().database_url`; update every surviving reference.
- [x] Run the full suite (`uv run pytest tests/`) and confirm green with no programmatic exclusions beyond the pre-existing CI set; report counts with any exclusions named.
- [x] Write the ADR extending ADR-003: the backend seam, deterministic backend selection, durability-per-backend (Litestream for SQLite, engine-managed for Postgres), and the SQLite→Postgres migration path, with only SQLite implemented now.
