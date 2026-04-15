# Design: Test Environment Hermeticity

## Context

`ConversionConfig` is a `pydantic_settings.BaseSettings` subclass configured via `SettingsConfigDict(env_file=".env", ...)`.
`BaseSettings` pulls from two sources at instantiation time:

1. An environment file on disk, controlled by the `env_file` setting or `_env_file` constructor kwarg.
2. Environment variables already present in `os.environ` — matched by each field's `validation_alias`.
   This source is active regardless of `_env_file`.

The FastAPI lifespan in [src/aizk/conversion/api/main.py:21](src/aizk/conversion/api/main.py#L21) constructs `ConversionConfig()` with no overrides, so any test that relies on the live app picks up both the developer's `.env` and any matching shell-exported variable.

Test-body configs vary: some already pass `_env_file=None`, others do not.
Even when a test body is disciplined about env-file handling, neither the body nor the app-side lifespan is protected against shell-exported vars — they can silently adopt workstation state.

The existing `set_test_env` autouse fixture in [tests/conversion/conftest.py](../../../tests/conversion/conftest.py) monkeypatches a fixed allowlist of infrastructure vars (database URL, S3/AWS creds, retry delay).
It does not touch any `DOCLING_*` alias or any other `ConversionConfig` field, leaving the shell-var leakage path open.

## Decisions

### Decision: Neutralize `.env` loading at the class level via an autouse fixture

Use an autouse, session-scoped pytest fixture (placed in [tests/conversion/conftest.py](../../../tests/conversion/conftest.py)) that mutates `ConversionConfig.model_config["env_file"] = None` for the duration of the test session.
This makes every `ConversionConfig()` instantiation — test-body or app-side — skip `.env` without requiring callers to pass `_env_file=None` each time.

**Rationale:**

- Covers both call sites (test body and FastAPI lifespan) with one mechanism.
- Doesn't require every test author to remember a kwarg; the fixture enforces the invariant.
- Keeps the fix out of production code — `ConversionConfig` continues to read `.env` in real runs.

**Alternatives considered:**

- _Fixture that overrides `app.state.config` after `create_app()` but before lifespan runs._
  Requires ordering guarantees between fixture and `TestClient` construction and doesn't cover direct `ConversionConfig()` calls in test bodies.
- _Change production `env_file` default to `None` and require explicit opt-in._
  Pushes the hermeticity concern into production code; developers would then have to remember to pass `_env_file=".env"` in real runs, which is the opposite of the usability we want at runtime.
- _Rely on every test to pass `_env_file=None`._
  Status quo.
  Doesn't cover the app-side lifespan config, and every new test is a fresh chance to reintroduce the bug.

### Decision: Strip shell-exported env vars matching `ConversionConfig` aliases at session start

In the same session-scoped fixture, introspect `ConversionConfig.model_fields`, collect every `validation_alias`, and delete each one from `os.environ` at session start (`os.environ.pop(alias, None)`), unless the alias is in an allowlist of vars the harness intentionally sets.
Restore the original values at session end.

**Rationale:**

- `BaseSettings` reads shell env vars directly from `os.environ` regardless of `_env_file`; the `.env`-level fix alone leaves this path open.
- Introspection via `model_fields` keeps the allowlist logic on the production config — new fields automatically inherit hermeticity without a test-side update.
- The allowlist captures what `set_test_env` already claims (database URL, S3/AWS creds, retry delay), so the two fixtures don't fight.
- Session scope is safe: `set_test_env` is function-scoped and uses `monkeypatch.setenv`, which restores the original value on test teardown — and the original value, after our deletion, is "unset," so monkeypatch correctly cleans up.

**Alternatives considered:**

- _Enumerate the allowlist by hand inside the hermeticity fixture._
  Rejected: duplicates knowledge of `ConversionConfig`'s fields; drifts the moment a new field is added.
- _Subclass `BaseSettings` to disable env-var reads in tests._
  Rejected: same objection as the `env_file` decision — production behavior should stay untouched.
- _Add a CI-only assertion that no `DOCLING_*` var is set before pytest runs._
  Rejected: only catches the problem in CI; local dev still gets contaminated.

### Decision: Keep direct `_env_file=None` kwargs in existing tests

Even with the autouse fixture, update the six direct-construct offenders to pass `_env_file=None`.

**Rationale:**

- The kwarg makes the intent visible at the call site; it documents that the test author thought about hermeticity.
- Provides defense in depth if the fixture is ever scoped too narrowly, misapplied, or bypassed (e.g., a test that opts out of autouse).
- Cost is one keyword argument per construction — negligible.

### Decision: Scope the fixture to `tests/conversion/` initially

Place the autouse fixture inside the existing [tests/conversion/conftest.py](../../../tests/conversion/conftest.py) rather than at the repo-root `tests/conftest.py`.

**Rationale:**

- `ConversionConfig` is the only `BaseSettings` subclass in `src/` today; no other module reads `.env` or otherwise depends on pydantic-settings, so a fixture at the conversion-tests scope covers 100% of current surface.
- Matches the existing `set_test_env` autouse fixture's scope, keeping test-harness plumbing in one place.
- The `testing` capability spec is project-wide, but the concrete mechanism is per-service — the spec doesn't mandate a single shared fixture.

## Architecture

```text
pytest session starts
    │
    ▼
tests/conversion/conftest.py::_hermetic_conversion_config (autouse, session-scope)
    │
    ├── 1. ConversionConfig.model_config["env_file"] = None
    │        (blocks .env parsing for every instance this session)
    │
    └── 2. for alias in ConversionConfig.model_fields.*.validation_alias:
            if alias not in harness_allowlist:
                os.environ.pop(alias, None)
        (blocks inheritance of shell-exported vars for unclaimed fields)
    │
    ▼
tests/conversion/conftest.py::set_test_env (autouse, function-scope)
    │
    └── monkeypatch.setenv for DATABASE_URL, S3_*, AWS_*, RETRY_BASE_DELAY_SECONDS
        (the allowlisted vars the harness owns)
    │
    ▼
test collection & execution
    │
    ├── test body: ConversionConfig()             ── no .env, no stray shell vars
    │
    └── test body: TestClient(create_app())
              │
              ▼
        FastAPI lifespan: ConversionConfig()      ── no .env, no stray shell vars
              │
              ▼
        app.state.config                          ── hermetic
```

## Risks

- **Fixture scoping mistake.**
  If the fixture is ever imported into a scope that doesn't cover all conversion tests (e.g., a sub-conftest overrides it), hermeticity regresses silently.
  _Mitigation:_ the autouse + session scope at [tests/conversion/conftest.py](../../../tests/conversion/conftest.py) covers the entire subtree.
  The redundant `_env_file=None` kwargs in direct-construct tests provide a second layer against the env-file path.

- **Allowlist drift.**
  If a new `ConversionConfig` field is added whose alias should be harness-managed, the hermeticity fixture will delete the shell value and `set_test_env` will not reset it — the test suite would then see library defaults for that field even when the harness intended otherwise.
  _Mitigation:_ keep the allowlist co-located with `set_test_env` in the same conftest, so the two fixtures are reviewed together when either changes.

- **Non-conversion tests added later.**
  The fixture only covers `tests/conversion/`.
  If new test trees appear for new services with their own `BaseSettings` subclasses, they must add their own equivalent fixture.
  _Mitigation:_ the spec states the contract project-wide; future services are on notice that they own their own hermeticity plumbing, and can copy the conversion-tests fixture as a template.

- **`model_config` mutation and `os.environ` deletion scope.**
  `pytest-xdist` and similar parallelization run each worker in its own process, so session-scope mutations are per-worker and do not leak.
  Single-process runs restore via the fixture's teardown.
  _No additional mitigation needed._
