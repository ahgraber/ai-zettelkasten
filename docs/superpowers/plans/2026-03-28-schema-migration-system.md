# Schema Migration System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Initialize Alembic with a baseline migration matching the current schema so all future schema changes go through versioned migration scripts.

**Architecture:** Alembic is configured with `alembic.ini` at the repo root and migration scripts under `src/aizk/conversion/migrations/`.
The `env.py` imports `SQLModel.metadata` (populated by the datamodel package) for autogenerate support.
The CLI `db-init` command switches from `create_all` to `alembic upgrade head`, making it idempotent for both fresh and existing databases.
Tests continue using `create_db_and_tables` for speed.

**Tech Stack:** Python, Alembic, SQLModel/SQLAlchemy, SQLite

---

## Task 1: Initialize Alembic configuration and env.py

**Files:**

- Create: `alembic.ini`

- Create: `src/aizk/conversion/migrations/__init__.py`

- Create: `src/aizk/conversion/migrations/env.py`

- Create: `src/aizk/conversion/migrations/script.py.mako`

- [ ] **Step 1: Create `alembic.ini` at the repo root**

```ini
[alembic]
script_location = src/aizk/conversion/migrations
prepend_sys_path = .

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console

[logger_sqlalchemy]
level = WARN
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
datefmt = %H:%M:%S
```

Note: `sqlalchemy.url` is intentionally omitted — `env.py` reads it from `ConversionConfig` at runtime.

- [ ] **Step 2: Create the migrations package**

Create an empty `src/aizk/conversion/migrations/__init__.py`:

```python
```

- [ ] **Step 3: Create `env.py`**

Create `src/aizk/conversion/migrations/env.py`:

```python
"""Alembic environment configuration for conversion service migrations."""

from __future__ import annotations

from alembic import context
from sqlalchemy import pool
from sqlmodel import SQLModel, create_engine

import aizk.conversion.datamodel  # noqa: F401 — registers models on SQLModel.metadata
from aizk.conversion.utilities.config import ConversionConfig

target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode for SQL script generation."""
    config = ConversionConfig()
    context.configure(
        url=config.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database connection."""
    config = ConversionConfig()
    connectable = create_engine(
        config.database_url,
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

Key details:

- `render_as_batch=True` enables SQLite ALTER TABLE support via batch mode.

- `import aizk.conversion.datamodel` ensures all SQLModel tables are registered on `SQLModel.metadata` before autogenerate runs.

- `ConversionConfig()` reads `DATABASE_URL` from environment / `.env` — no hardcoded URL.

- `pool.NullPool` avoids connection pooling during migrations (standard Alembic practice).

- [ ] **Step 4: Create `script.py.mako`**

Create `src/aizk/conversion/migrations/script.py.mako`:

```mako
"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
${imports if imports else ""}

# revision identifiers, used by Alembic.
revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    """Apply schema changes."""
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """Revert schema changes."""
    ${downgrades if downgrades else "pass"}
```

- [ ] **Step 5: Create the `versions/` directory**

Create an empty `src/aizk/conversion/migrations/versions/.gitkeep` file (empty file, no content).

- [ ] **Step 6: Verify Alembic recognizes the configuration**

Run: `uv run alembic heads`

Expected: Output shows no heads (no migrations yet), no errors about configuration.

- [ ] **Step 7: Commit**

```bash
git add alembic.ini src/aizk/conversion/migrations/
git commit -S -m "chore(conversion): initialize Alembic migration configuration

Configure env.py to read database URL from ConversionConfig and use
SQLModel.metadata for autogenerate. Batch mode enabled for SQLite
ALTER TABLE support."
```

---

## Task 2: Generate and verify baseline migration

**Files:**

- Create: `src/aizk/conversion/migrations/versions/<revision>_baseline.py` (generated by Alembic)

- [ ] **Step 1: Generate the baseline migration**

Run: `uv run alembic revision --autogenerate -m "baseline"`

This generates a migration in `src/aizk/conversion/migrations/versions/` that creates the three tables (`bookmarks`, `conversion_jobs`, `conversion_outputs`) with all columns, indexes, and foreign keys.

- [ ] **Step 2: Review the generated migration**

Open the generated file and verify it contains `op.create_table` calls for all three tables:

1. `bookmarks` — 10 columns, unique constraints on `karakeep_id` and `aizk_uuid`, indexes on `karakeep_id`, `aizk_uuid`, `normalized_url`
2. `conversion_jobs` — 16 columns, FK to `bookmarks.aizk_uuid`, unique on `idempotency_key`, indexes on `aizk_uuid`, `status`, `idempotency_key`, `earliest_next_attempt_at`, `created_at`
3. `conversion_outputs` — 13 columns, FK to `conversion_jobs.id` (unique) and `bookmarks.aizk_uuid`, indexes on `job_id`, `aizk_uuid`, `markdown_hash_xx64`, `created_at`

The `downgrade()` function should contain `op.drop_table` calls in reverse order (outputs → jobs → bookmarks).

If any table or column is missing, check that `aizk.conversion.datamodel` is imported in `env.py`.

- [ ] **Step 3: Test upgrade on a fresh database**

Run: `DATABASE_URL=sqlite:///./data/test_migration.db uv run alembic upgrade head`

Expected: No errors.
The database file is created with all three tables.

- [ ] **Step 4: Verify schema matches create_all**

Run the following to compare the migrated schema against `create_all`:

```bash
uv run python -c "
from sqlmodel import SQLModel, create_engine, inspect
import aizk.conversion.datamodel  # noqa: F401

# create_all schema
e1 = create_engine('sqlite:///./data/test_create_all.db')
SQLModel.metadata.create_all(e1)

# migration schema
e2 = create_engine('sqlite:///./data/test_migration.db')

i1, i2 = inspect(e1), inspect(e2)
for table in ['bookmarks', 'conversion_jobs', 'conversion_outputs']:
    cols1 = {c['name'] for c in i1.get_columns(table)}
    cols2 = {c['name'] for c in i2.get_columns(table)}
    assert cols1 == cols2, f'{table} columns differ: {cols1 ^ cols2}'
    print(f'{table}: {len(cols1)} columns — OK')
print('Schema match verified.')
"
```

Expected: All three tables match, script prints "Schema match verified."

- [ ] **Step 5: Test downgrade**

Run: `DATABASE_URL=sqlite:///./data/test_migration.db uv run alembic downgrade base`

Expected: No errors.
All tables are dropped.

- [ ] **Step 6: Clean up test databases**

```bash
rm -f ./data/test_migration.db ./data/test_create_all.db
```

- [ ] **Step 7: Commit**

```bash
git add src/aizk/conversion/migrations/versions/
git commit -S -m "feat(conversion/db): add baseline migration for existing schema

Autogenerated migration creates bookmarks, conversion_jobs, and
conversion_outputs tables matching the current SQLModel definitions."
```

---

## Task 3: Wire Alembic into CLI

**Files:**

- Modify: `src/aizk/conversion/cli.py`

- Modify: `src/aizk/conversion/api/main.py`

- [ ] **Step 1: Update `_cmd_db_init` to run Alembic upgrade**

In `src/aizk/conversion/cli.py`, replace the `_cmd_db_init` function:

```python
def _cmd_db_init(_args: argparse.Namespace) -> int:
    """Initialize database tables via Alembic migrations."""
    setproctitle("docling-db-init")
    from alembic import command
    from alembic.config import Config

    alembic_cfg = Config("alembic.ini")
    command.upgrade(alembic_cfg, "head")
    return 0
```

`env.py` reads the database URL from `ConversionConfig` (via environment), so no URL override is needed here.

Remove the `create_db_and_tables` and `get_engine` imports from `cli.py` — neither is used after this change:

```python
# DELETE: from aizk.conversion.db import create_db_and_tables, get_engine
```

Also remove the `config = ConversionConfig()` line from `_cmd_db_init` — it's no longer needed since `env.py` handles config.
Keep the `ConversionConfig` import since it's still used by `_cmd_serve` and `_cmd_worker`.

- [ ] **Step 2: Update API lifespan to run Alembic upgrade**

In `src/aizk/conversion/api/main.py`, update the lifespan to use Alembic:

```python
@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Initialize resources needed for the API lifespan."""
    from alembic import command
    from alembic.config import Config

    config = ConversionConfig()
    _app.state.config = config
    configure_logging(config)
    configure_mlflow_tracing(
        enabled=config.mlflow_tracing_enabled,
        tracking_uri=config.mlflow_tracking_uri,
        experiment_name=config.mlflow_experiment_name,
    )
    alembic_cfg = Config("alembic.ini")
    command.upgrade(alembic_cfg, "head")
    yield
```

Remove unused imports:

```python
# DELETE: from aizk.conversion.db import create_db_and_tables, get_engine
```

Note: `config = ConversionConfig()` remains here because it's still needed for `_app.state.config`, logging, and MLflow.

- [ ] **Step 3: Run existing tests**

Run: `uv run pytest tests/conversion/ -v`

Expected: All PASS.
Tests use `create_db_and_tables` via the `db_engine` fixture in `conftest.py`, which is unaffected by the CLI/lifespan changes.

- [ ] **Step 4: Commit**

```bash
git add src/aizk/conversion/cli.py src/aizk/conversion/api/main.py
git commit -S -m "refactor(conversion): switch db-init and API startup to Alembic migrations

Both _cmd_db_init and the API lifespan now run alembic upgrade head
instead of create_all. Tests continue using create_db_and_tables for
speed and isolation."
```

---

## Task 4: Add migration round-trip test

**Files:**

- Create: `tests/conversion/unit/test_migrations.py`

- [ ] **Step 1: Write the migration test**

Create `tests/conversion/unit/test_migrations.py`:

```python
"""Tests for Alembic migration integrity."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlmodel import SQLModel, create_engine, inspect

import aizk.conversion.datamodel  # noqa: F401


def _alembic_cfg(database_url: str) -> Config:
    """Return an Alembic config pointing at the given database."""
    repo_root = Path(__file__).resolve().parents[3]
    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def test_upgrade_produces_schema_matching_create_all(tmp_path):
    """Verify that running all migrations produces the same schema as create_all."""
    migrated_url = f"sqlite:///{tmp_path / 'migrated.db'}"
    baseline_url = f"sqlite:///{tmp_path / 'baseline.db'}"

    # Schema via migrations
    command.upgrade(_alembic_cfg(migrated_url), "head")

    # Schema via create_all
    baseline_engine = create_engine(baseline_url)
    SQLModel.metadata.create_all(baseline_engine)

    migrated_inspector = inspect(create_engine(migrated_url))
    baseline_inspector = inspect(baseline_engine)

    migrated_tables = set(migrated_inspector.get_table_names())
    baseline_tables = set(baseline_inspector.get_table_names())
    # Alembic adds alembic_version; filter it out
    migrated_tables.discard("alembic_version")

    assert migrated_tables == baseline_tables, f"Table mismatch: {migrated_tables ^ baseline_tables}"

    for table in baseline_tables:
        baseline_cols = {c["name"] for c in baseline_inspector.get_columns(table)}
        migrated_cols = {c["name"] for c in migrated_inspector.get_columns(table)}
        assert baseline_cols == migrated_cols, f"{table} column mismatch: {baseline_cols ^ migrated_cols}"


def test_upgrade_downgrade_round_trip(tmp_path):
    """Verify that upgrade then downgrade leaves no tables behind."""
    db_url = f"sqlite:///{tmp_path / 'roundtrip.db'}"
    cfg = _alembic_cfg(db_url)

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")

    inspector = inspect(create_engine(db_url))
    remaining = set(inspector.get_table_names()) - {"alembic_version"}
    assert remaining == set(), f"Tables remain after downgrade: {remaining}"
```

- [ ] **Step 2: Run the migration tests**

Run: `uv run pytest tests/conversion/unit/test_migrations.py -v`

Expected: Both tests PASS.

- [ ] **Step 3: Run full test suite**

Run: `uv run pytest tests/conversion/ -v`

Expected: All PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/conversion/unit/test_migrations.py
git commit -S -m "test(conversion/db): add migration round-trip and schema parity tests

Verify that Alembic migrations produce the same schema as create_all
and that upgrade/downgrade round-trips cleanly."
```
