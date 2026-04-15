# Proposal: Test Environment Hermeticity

## Intent

Test outcomes must not depend on the developer's workstation configuration state.
`pydantic_settings.BaseSettings` reads two configuration sources by default: a `.env` file on disk, and environment variables already present in the shell when pytest starts.
Either source can contaminate a test run — a populated `.env` flows into the FastAPI app-side `ConversionConfig` while test-body configs use `_env_file=None` and diverge, and a shell-exported variable (e.g., `DOCLING_PICTURE_DESCRIPTION_MODEL` set in the developer's profile) flows into every config instance regardless of `_env_file`.
Both produce spurious failures that reproduce only on machines with certain configuration state.

The hole is broad: test files that use `create_app()` / `TestClient` have no fixture overriding `app.state.config` before the lifespan runs, several test files construct `ConversionConfig()` directly without `_env_file=None`, and nothing in the test harness removes pre-existing shell env vars that `ConversionConfig` would consume.

## Scope

**In:**

- Establish a test-harness contract that test runs are hermetic with respect to workstation configuration state from either source (`.env` file or pre-existing shell env vars).
- Add conftest mechanisms that (a) guarantee the FastAPI app's lifespan sees a `.env`-free `ConversionConfig` when the app is built inside a test, and (b) remove any shell-exported env var that `ConversionConfig` would otherwise consume, except for vars the test harness explicitly sets.
- Fix the direct offenders constructing `ConversionConfig()` without `_env_file=None`.

**Out:**

- Changing `ConversionConfig` production defaults or its pydantic-settings configuration.
- Restructuring the broader test layout, fixture hierarchy, or per-service conftest boundaries.
- Extending hermeticity to other external state (filesystem, network, docker) — this change is scoped to configuration.
- Lint or static-analysis enforcement; the contract is stated in spec and scenarios, not mechanically blocked.

## Approach

Introduce a new `testing` capability whose contract states the hermeticity principle without prescribing a specific mechanism.
Mechanism lives in `design.md` and code:

- An autouse, session-scoped fixture in `tests/conversion/conftest.py` (a) sets `ConversionConfig.model_config["env_file"] = None` for the session — covers both test-body and app-side constructions — and (b) deletes every shell env var matching any `ConversionConfig` validation alias, except aliases already claimed by the existing `set_test_env` fixture.
- Direct `ConversionConfig()` constructions in unit tests are updated to pass `_env_file=None` as visible defense in depth.
- No production code changes.
