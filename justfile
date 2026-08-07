# AI-Zettelkasten task runner — see `just --list`.
#
# Credentials
# -----------
# Recipes that need live credentials run under `op run`, which resolves the
# `op://` references in `.env.op` into the child process environment. Those
# values are injected before `.env` is read, so they win over `.env` and are
# available for the `${_VAR}` interpolations that `.env` performs.
#
# To run without 1Password (credentials already exported, or concrete values
# in `.env`), clear the prefix:
#
#     just op= serve

op := "op run --env-file=.env.op --"

# List available recipes.
default:
    @just --list

# ---------------------------------------------------------------- setup ---

# Install the project and its default dependency groups.
install:
    uv sync

# Install the root project plus every workspace member (notebooks/claimify).
install-all:
    uv sync --all-packages

# Check that every `op://` reference in `.env.op` resolves. Prints no secrets.
secrets-check:
    @{{ op }} true && echo "all .env.op references resolved"

# ------------------------------------------------------------- database ---

# Create the shared conversion + graph tables. Run once; workers also migrate.
db-init:
    {{ op }} uv run aizk-conversion db-init

# ----------------------------------------------------- conversion stage ---

# Run the conversion API on AIZK_API_HOST:AIZK_API_PORT (default 0.0.0.0:8000).
serve:
    {{ op }} uv run aizk-conversion serve

# Run the conversion worker loop.
worker:
    {{ op }} uv run aizk-conversion worker

# ---------------------------------------------------------- graph stage ---

# Run the graph operator API + console (default 0.0.0.0:8001, at /ui).
graph-serve:
    {{ op }} uv run aizk-graph serve

# Run the contextualization worker loop.
graph-worker:
    {{ op }} uv run aizk-graph worker

# Run the mention-extraction worker loop.
extraction-worker:
    {{ op }} uv run aizk-graph extraction-worker

# Pre-fetch the pinned GLiNER2 weights into the local model directory.
fetch-gliner2-weights:
    {{ op }} uv run aizk-graph fetch-gliner2-weights

# Run a foreground extraction pass, e.g. `just extract-dataset --limit 10 --yes`.
extract-dataset *ARGS:
    {{ op }} uv run aizk-graph extract-dataset {{ ARGS }}

# ------------------------------------------------------------- quality ---

# Run the test suite in parallel, excluding the subprocess lifecycle tests.
test *ARGS:
    uv run pytest -n auto -m "not integration_lifecycle" tests/ {{ ARGS }}

# Run the subprocess lifecycle tests; these are incompatible with xdist.
test-lifecycle *ARGS:
    uv run pytest -m integration_lifecycle tests/ {{ ARGS }}

# Run both test suites.
test-all: test test-lifecycle

# Run the parallel suite with a coverage report.
coverage:
    uv run pytest -n auto -m "not integration_lifecycle" --cov=src --cov-report=term-missing tests/

# Lint and format with ruff.
format:
    uv run ruff check --fix .
    uv run ruff format .

# Run every pre-commit hook over the whole tree.
lint:
    prek run --all-files

# ---------------------------------------------------------- containers ---

# Build and start the API + worker containers.
up:
    podman-compose -f containers/podman-compose.yaml up -d --build

# Stop the containers.
down:
    podman-compose -f containers/podman-compose.yaml down

# Follow container logs.
logs *ARGS:
    podman-compose -f containers/podman-compose.yaml logs -f {{ ARGS }}

# ------------------------------------------------------------- release ---

# Preview a release. BUMP is one of: major | minor | patch.
release-dry BUMP:
    uv run uv-ship --dry-run next {{ BUMP }}

# Cut a release: bump version, update CHANGELOG, commit, tag, push.
release BUMP:
    uv run uv-ship next {{ BUMP }}
