# Instructions

Deliver exactly what was requested.
Avoid speculative extras, but include the minimum tests, documentation, and safeguards needed to keep behavior correct and prevent regressions.

## Directive Priority

If directives conflict, prioritize:

1. Correctness and safety at external boundaries
2. Explicit user instructions
3. Minimal scope and simplicity

## 1. Think Before Coding

Objective: surface ambiguity and tradeoffs before writing any code.

- State assumptions explicitly.
- If uncertainty would materially change the implementation, ask.
  Otherwise, state your assumption and proceed.
- If multiple interpretations exist, present them — don't pick silently.
- If a simpler approach exists, say so.
  Push back when warranted.

## 2. Simplicity First

Objective: write the _minimum change_ that meets the request.

IMPORTANT: Preserve the original code and the logic of the original code as much as possible.

- No features, abstractions, or configurability beyond what was asked.
- No "flexibility" or "configurability" that wasn't requested.
- Prefer the golden path for internal logic; let tests define edge-case expectations.
- Add explicit validation and error handling at external boundaries (I/O, network, persistence, auth, parsing, external APIs).
- If you write 200 lines and it could be 50, rewrite it.
- Apply YAGNI ruthlessly.

## 3. Surgical Changes

Objective: every changed line traces directly to the request.

When editing existing code:

- Touch only what the request requires.
  Don't "improve" adjacent code, comments, or formatting.
- Match existing style, even if you'd do it differently.
  Don't refactor existing code unless it is part of the request.
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked — mention it instead.

## 4. Goal-Driven Execution

Objective: define success criteria, then loop until verified.

Transform tasks into verifiable goals:

- "Add validation" → write tests for invalid inputs, then make them pass.
- "Fix the bug" → write a test that reproduces it, then make it pass.
- "Refactor X" → ensure tests pass before and after.

Testing guardrails:

- Never modify a failing test to make it pass.
  Fix the code under test.
- If a test is genuinely wrong, explain why and await user approval before changing it.
- Write implementations that solve the general problem, not code that special-cases specific test inputs.

For multi-step tasks, state a brief plan defining the step task and associated verification checks.

## 5. Definition of Done

- The requested behavior works as specified.
- Behavior changes are covered by tests, or testing gaps are explicitly stated.
- Public contract changes are documented.
- Required checks were run when available; if not run, state what was skipped and why.

## Defaults

- Review available skills for relevance before writing code.
- If intermediate, user-aligned work is needed before final output, surface in `._scratch/`.
- If worktrees are warranted use a local .worktrees/ directory.
- Use descriptive, consistent naming conventions.
- Write docstrings or comments for public contracts and non-obvious behavior.
- Use type annotations where the language supports them.
- Use structured logging where the project uses logging.
- Run lint/format/test through project tooling when available; do not hand-format code.
- Write tests for public behavior and regressions, not implementation details.

## Technology & Data Handling Requirements

- Python code runs in the uv-managed environment; dependencies are pinned via uv/lockfiles (pyproject.toml + uv.lock) and honored by Nix devshells — no ad-hoc global installs.
- Default runtime targets CPU-only execution; GPU use requires an explicit cost and ops justification.
- Storage of raw inputs and derived artifacts must permit replay; blob/object storage locations are recorded alongside metadata.
- Secret management: Secrets/keys MUST NOT be committed.
  Store them in a gitignored `.env` file (or a secret manager) and access them via environment variables at runtime.
- Process identification: Every Python process MUST set a descriptive process title using `setproctitle` so hosts running multiple Python processes can distinguish them.

## Workflow & Quality Gates

- Use Spec-Driven Development and Test-Driven Development.
- Any change to data schemas, embedding parameters, or retrieval scoring requires a migration/test plan and version bump of the affected artifact.
- Code review checks for reproducibility (pinned deps, seeded operations), privacy adherence, and observability hooks (structured logs + metrics).
- Techstack/tooling choices for external services or internal frameworks MUST reference an ADR in `docs/decision-record/` or be manually overridden with references to other documents.
- Significant architectural decisions MUST have an ADR.

## Architecture Patterns

Apply these patterns when designing or changing asynchronous jobs, processing stages, ingestion flows, derived artifacts, external-service integrations, or operator-facing runtime behavior.
Keep them general: name the role a component plays, not the current stage that happens to use it.

- Separate the **domain core** from the **orchestration engine**.
  Domain code owns the unit of work, input/output contracts, validation, provenance, artifact writes, and outcome classification.
  The engine owns work discovery, claim/lease, eligibility ordering, retry scheduling, concurrency, cancellation, timeout, draining, and stale-work recovery.
- Treat the current in-process or database-backed runner as one engine implementation, not as a permanent framework.
  Do not build an engine-neutral meta-framework.
  If an outside orchestrator is adopted later, replace the engine layer and keep the domain contracts narrow enough to survive.
- Prefer task-oriented units with explicit inputs and outputs.
  A task should be a self-contained verb that does not assume a specific upstream caller, downstream consumer, execution order, or failure policy beyond its declared contract.
- Keep orchestration state small and replayable.
  Pass durable IDs, cursors, page references, and blob/object locations across boundaries; keep large payloads in the project-owned store and record their locations with metadata.
- Version model-dependent or configuration-dependent derived state.
  Record input fingerprints, producing versions, and generation/run identifiers; make produced rows immutable except for explicit lifecycle transitions; activate a new generation and retire the prior one atomically; plan compaction/retention before unbounded history becomes a storage problem.
- Match identity to determinism.
  Deterministic artifacts may use content-derived identities where ID churn is the invalidation signal.
  Non-deterministic, model-dependent, or configuration-dependent artifacts should use generation-scoped identities or explicit membership rows.
- Keep lifecycle, generation, provenance, and lineage as distinct mechanisms.
  An active/superseded generation pointer is not a substitute for append-only lineage when identities can split, merge, or remain resolvable after retirement.
- When an ADR names a future datastore, orchestrator, or runtime target, preserve invariants in that target's dialect or document the gap explicitly.
  Do not let a current-backend implementation silently weaken a claimed migration path.
- Co-commit state changes with the events that describe them when they are in the same datastore.
  Treat event tables and status projections as product read models, not as a substitute for business state.
  Operator surfaces should query project-owned projections, not an orchestrator's internal history.
- Under an external workflow engine, assume at-least-once activity execution across the engine/application boundary.
  Projection writes, artifact writes, and cleanup must be idempotent; do not rely on cross-store atomicity between the engine's progress marker and the application database.
- Classify failures at the boundary where the stage understands them.
  Retry at exactly one layer, prefer the cheapest safe retry layer, and map outcomes into a small generic lifecycle before the engine reasons about scheduling.
- Bound fan-out with explicit concurrency/rate controls.
  Execution concurrency is not write concurrency: keep commits short, batch writes by logical unit, and preserve the single-writer SQLite/Litestream assumption until an ADR changes it.
- External provider clients must be injectable.
  When more than one consumer can hit the same provider, share rate limits, budgets, caching, backoff, and observability through the injected client layer rather than constructing isolated clients inside tasks.
- Use durable polling or pull-based repair by default.
  Do not add pub/sub, global write-admission controllers, LLM gateways, or new orchestration services without a measured need, a named missing primitive, and an ADR.
- Adopt new orchestration technology for a missing capability primitive, not for abstraction neatness.
  Examples of valid triggers include durable signals, human approval waits, long sleeps, schedules with overlap policy, step checkpointing, or scale limits demonstrated by measurement.
- Preserve a correlation spine across logs, events, metrics, and artifacts.
  Include stable source identity, generation/run identity, component/task name, work-unit reference, attempt, and process identity where applicable.

## Governance Practices

- Semantic Versioning is REQUIRED (MAJOR.MINOR.PATCH).
- Conventional Commits are REQUIRED for commit messages and/or PR titles.
- Keep a Changelog is REQUIRED; maintain `CHANGELOG.md` following <https://keepachangelog.com>.
- After the first MINOR release, all changes affecting data/schema/contracts MUST include a migration plan and a deprecation schedule.

## Testing

- Run tests via `uv run pytest tests/`.
- For parallel execution, use `pytest-xdist`: `uv run pytest -n auto -m "not integration_lifecycle" tests/`.
- Tests marked `integration_lifecycle` (subprocess lifecycle with `pytest-isolate`) are incompatible with xdist; run them separately: `uv run pytest -m integration_lifecycle tests/`.
- Do not use `pytest-run-parallel` — it is a thread-safety stress tester (runs the same test N times in N threads), not a test suite parallelizer.

### Resource leak detection with pyleak

Use [pyleak](https://github.com/deepankarm/pyleak) to guard against leaked asyncio tasks and threads.
When writing tests for code that spawns concurrent work, wrap the act phase with the appropriate pyleak context manager:

- `no_task_leaks(action="raise")` — for code using `asyncio.create_task`, `asyncio.gather`, `asyncio.to_thread`, or `TaskGroup`.
- `no_thread_leaks(action="raise")` — for code using `ThreadPoolExecutor`, `threading.Thread`, or `subprocess.Popen` lifecycle management.

Existing examples: `test_fetcher.py`, `test_async_utils.py`, `test_limiters.py`, `test_health_checks.py`, `test_worker_shutdown.py`.

## Commit & Review Guidelines

- **Hard gate before committing**: before running `git agent-commit`, present the user with (1) the proposed commit message and (2) a concise diff summary covering which files changed and what each change does.
  Wait for explicit user approval; do not proceed if the user requests changes.
- **Every commit message draft, without exception, must be produced by invoking the `commit-message` skill first.**
  A prior invocation earlier in the same session does not satisfy this requirement — re-invoke for each request.
  Drafting inline, from memory, or from habit is not acceptable.
- Commit format: `type(scope): summary` (e.g., `feat(zsh): …`, `fix(vscode): …`).
  Scope should reflect directories or logical surfaces.
- Separate unrelated changes (docs vs configs vs lockfile updates) into distinct commits.
- Use `git agent-commit` (not `git commit`) to create signed commits; this alias uses the dedicated agent signing key at `~/.ssh/id_ed25519_agent_signing`.

## Sandbox Limitations

- The sandbox cannot run `uv sync` or read `.env` / `.env.example` (permission errors).
<!-- - `tests/conversion/conftest.py` imports `aizk.conversion.db` → `pydantic_settings`, which may fail with `ModuleNotFoundError: No module named 'pydantic_settings.sources.providers.secrets'` if sandbox permissions are too strict. -->
- **Delegate test runs to the user** when any of the above errors occur.
  Describe the exact command to run (e.g., `uv run pytest tests/...`).
