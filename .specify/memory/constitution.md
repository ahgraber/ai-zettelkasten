<!--
Sync Impact Report
- Version: 1.2.0 → 1.3.0
- Modified Principles/Sections: Technology requirements updated with process naming via setproctitle
- Added Sections: none
- Removed Sections: none
- Templates requiring updates: none
- Follow-up TODOs: none
-->

# AI-Zettelkasten Constitution

## Core Principles

### Data Provenance & Integrity

Every ingested or generated artifact MUST carry source metadata (origin URL or system identifier), hashing for integrity (content hash + optional semantic hash), and a replayable link to the extraction process. Lossy transforms (e.g., OCR, PDF parsing) require stored raw inputs and parsing parameters to allow reprocessing; raw inputs MAY remain in an authoritative external system if access is stable and a durable reference is recorded. No orphaned nodes: every note links back to provenance and references.

### Reproducible Pipelines

All pipelines (ingest, parse, chunk, embed, index) MUST be deterministic and idempotent: pinned dependencies, versioned configs, fixed seeds, and execution inside the Nix/uv environment defined in the repo. Re-running with the same inputs and configs produces the same outputs without duplicate side effects. Outputs must be regenerable from inputs + config; any non-determinism (remote models/APIs) requires recorded model/version and request parameters plus stable fallback fixtures for tests.

### Test-Driven Development

Test-driven development is required unless explicitly overridden for a given task. Overrides must be granular (task and/or module level, not feature level). In test-driven development, it is non-negotiable that no implementation code shall be written before:

1. Unit tests are written
2. Tests are validated and approved by the user
3. Tests are confirmed to FAIL (Red phase)

Parsing, chunking, embedding, and indexing changes MUST be covered by automated tests before implementation is merged. Contract or integration tests guard external services; fixture-based tests guard deterministic transforms; regression tests are added for any production defect. Tests must run via the documented dev shell with clear pass/fail signals in CI.

### Privacy & Safety Guardrails

Personally identifiable or sensitive data is NOT persisted without explicit consent or contractual basis; redact at ingest when possible. Secrets stay in environment configuration; access controls are required for corpora with restrictions. External API calls must avoid transmitting sensitive text unless contractually approved and logged.

### Observability & Versioned Artifacts

Structured logging (timestamps, component, correlation IDs) is required across pipelines. Metrics capture ingest throughput, parse success/failure, embedding coverage, and index freshness. Datasets, models, and embeddings are versioned; any incompatible schema or scoring change includes a migration note and version bump.

## Technology & Data Handling Requirements

- Python code runs in the uv-managed environment; dependencies are pinned via uv/lockfiles (pyproject.toml + uv.lock) and honored by Nix devshells—no ad-hoc global installs.
- Default runtime targets CPU-only execution; GPU use requires an explicit cost and ops justification.
- Storage of raw inputs and derived artifacts must permit replay; blob/object storage locations are recorded alongside metadata.
- Minimal infrastructure bias: compose/k3s-friendly deployments; avoid services that break portability unless justified.
- Backend defaults: use FastAPI for service endpoints.
- Data layer defaults: use SQLite via SQLModel; any deviation or external service selection MUST have a recorded ADR.
- Secret management: Secrets/keys MUST NOT be committed. Store them in a gitignored `.env` file (or a secret manager) and access them via environment variables at runtime.
- Process identification: Every Python process MUST set a descriptive process title using `setproctitle` so hosts running multiple Python processes can distinguish them.

## Workflow & Quality Gates

- Implementation is initiated only after `spec.md` and `plan.md` are defined and approved.
- Every PR documents which principles are impacted and how compliance is verified.
- CI must run automated tests relevant to the touched pipeline stage; failures block merge.
- Any change to data schemas, embedding parameters, or retrieval scoring requires a migration/test plan and version bump of the affected artifact.
- Code review checks for reproducibility (pinned deps, seeded operations), privacy adherence, and observability hooks (structured logs + metrics).
- Techstack/tooling choices for external services or internal frameworks MUST reference an ADR in `docs/decision-record/` or be manually overridden with references to other documents.
- Significant architectural decisions MUST have an ADR.

## Governance

The constitution supersedes other practice docs when in conflict. Amendments require an ADR or PR summary that states the change, rationale, impact to principles, and updates to affected templates/specs/tasks.

Versioning and change management:

- Semantic Versioning is REQUIRED (MAJOR.MINOR.PATCH).
- Conventional Commits are REQUIRED for commit messages and/or PR titles.
- Keep a Changelog is REQUIRED; maintain `CHANGELOG.md` following https://keepachangelog.com.
- After the first MINOR release, all changes affecting data/schema/contracts MUST include a migration plan and a deprecation schedule.

Versioning of this constitution follows semantic rules: MAJOR for breaking governance changes, MINOR for added/expanded principles or sections, PATCH for clarifications. All merges must confirm Constitution Check gates in planning documents are satisfied or waived with rationale.

## Engineering Standards

### Key Principles

- Write clean, readable, and well-documented code.
- Prioritize simplicity, clarity, and explicitness in code structure and logic.
- Overly defensive programming leads to overcomplication - program for the minimal golden path and expand defense only where unit tests indicate need.
- Follow the Zen of Python and adopt pythonic patterns.
- Focus on modularity and reusability, organizing code into functions, classes, and modules; favor composition over inheritance.
- Optimize for performance and efficiency; avoid unnecessary computations and prefer efficient algorithms.
- Ensure proper error handling and structured logging for debugging.

### Style Guidelines

- Use descriptive and consistent naming conventions (snake_case for functions/variables, PascalCase for classes, UPPER_SNAKE_CASE for constants).
- Provide clear Google-style docstrings for public functions, classes, and modules with usage, parameters, and return values.
- Use type hints to improve readability and enable static analysis.
- Use f-strings for general formatting, and %-formatting for logs.
- Use environment variables for configuration management.
- Do not lint or format code manually; automated tooling runs on save/commit or can be invoked using the `ruff` CLI tool.

**Version**: 1.3.0 | **Ratified**: 2025-12-23 | **Last Amended**: 2025-12-25
