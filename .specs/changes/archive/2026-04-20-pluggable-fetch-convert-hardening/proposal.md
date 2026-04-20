# Proposal: pluggable-fetch-convert-hardening

## Intent

`pluggable-fetch-convert` shipped with a set of correctness, security, and encapsulation gaps identified during pre-merge code review.
The critical and high-severity items were partially resolved in hotfix commits; this change closes the remaining seven findings as a single consolidated follow-up so they can be tracked, reviewed, and verified together before `IngressPolicy` is widened beyond `karakeep_bookmark`.

## Scope

**In scope:**

- Seal `Orchestrator` DI encapsulation: add `config_snapshot: dict` and `converter_name: str`
  to `ProcessResult` so workers do not call the private `_resolve_converter` method (H2).
- Narrow `UrlRef._normalize` exception handling: hoist the `normalize_url` import to module
  scope; catch only `ValueError` / `validators.ValidationError` at call time;
  keep a deterministic fallback (`strip` + `casefold` + `rstrip("/")`) (H3).
- Enforce `source_ref` / `source_ref_hash` NOT NULL at the database layer: new Alembic
  revision asserting column population and altering both columns to NOT NULL;
  align SQLModel field definitions to remove `nullable=True` (H5).
- Settings hermeticity at runtime: build `DoclingConverterConfig` and `IngressPolicy` once at
  the composition root; route them through `app.state`; change all `BaseSettings` subclasses
  to default `env_file=None` so `.env` is never read outside the composition root (H7).
- `KarakeepBookmarkRef.bookmark_id` input validation: constrain to `^[A-Za-z0-9_-]{1,64}$`
  via `Field(pattern=...)`; apply `.strip()` in `to_dedup_payload()` (H9).
- Dedup canonicalization consistency: apply uniform whitespace/case normalization across all
  `to_dedup_payload()` implementations; add property-based test pinning stability (M3).
- Complete `Bookmark → Source` rename: remove remaining `as Bookmark` / `as BookmarkRecord`
  aliases in routes and uploader (M11).

**Out of scope:**

- Wiring `GithubReadmeRef.branch` through to the fetcher (accepted-but-ignored; deferred per spec).
- Queue-cap double-commit race (M2) — pre-existing pattern, deferred.
- `SecretStr` wrapping for API-key fields (M4) — deferred.
- Docling tempdir cleanup (M6) — deferred.
- Shared DAG traversal primitive across three walk sites (M12) — deferred.
- Subprocess traceback size cap (M14) — deferred.
- Any widening of `IngressPolicy.accepted_submission_kinds`.

## Approach

**H2 — ProcessResult shape:** `ProcessResult` (defined in `core/orchestrator.py`) gains `converter_name: str` and `config_snapshot: dict[str, Any]` written by the orchestrator before returning to the worker caller.
The worker's re-resolve call (`runtime.orchestrator._resolve_converter(...)`) is deleted; it reads both fields directly from the result.

**H3 — UrlRef.\_normalize:** Move the `from aizk.utilities.url_utils import normalize_url` import to module scope so import failures surface at startup.
In the validator body, catch only `ValueError` and `validators.ValidationError` (the error types `normalize_url` is documented to raise).
Fallback is `stripped.casefold().rstrip("/")` — deterministic across environments, not bare `.strip()`.

**H5 — NOT NULL migration:** SQLite requires a full-table rebuild to add NOT NULL without a default; the Alembic migration uses the batch-mode `op.batch_alter_table` pattern.
Before rebuilding, an assertion step counts rows with NULL `source_ref` or `source_ref_hash` and raises `IrreversibleMigrationError` if any exist (guarding against a partially-backfilled database).
The `Source` SQLModel definition drops `nullable=True` from both fields and removes `str | None` typing; the `model_validator` from the previous fix remains as a defense-in-depth check.

**H7 — Settings hermeticity:** `build_api_runtime` and `build_worker_runtime` each construct `DoclingConverterConfig` and `IngressPolicy` once (with `_env_file=None` for test callers) and attach them to `app.state` (for the API) or pass them directly (for the worker).
All `BaseSettings` subclasses in `utilities/config.py` and `wiring/ingress_policy.py` have their `model_config` changed to `SettingsConfigDict(..., env_file=None)`; the composition roots explicitly load `.env` via `dotenv.load_dotenv()` at process startup.

**H9 + M3 — Validation and canonicalization:** `bookmark_id` validation at the Pydantic model boundary prevents malformed IDs from reaching the DB or outbound HTTP calls.
A shared `_strip_and_casefold(s: str) -> str` helper (or inline `.strip().casefold()` for simple cases) is used consistently across variants' `to_dedup_payload()` methods; `owner` and `repo` on `GithubReadmeRef` are lowercased (GitHub org/repo names are case-insensitive).

**M11 — Rename cleanup:** Mechanical find-and-replace of `as Bookmark` / `as BookmarkRecord` import aliases and downstream `bookmark` local variable names.
No behavioral change.
