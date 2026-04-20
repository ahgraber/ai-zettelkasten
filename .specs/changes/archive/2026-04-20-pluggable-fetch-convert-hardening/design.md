# Design: pluggable-fetch-convert-hardening

## Context

This change closes seven hardening findings from the `pluggable-fetch-convert` pre-merge review.
All changes are within the existing `src/aizk/conversion/` module boundary; no new packages or external dependencies are introduced.
The findings fall into four independent clusters that can be implemented and reviewed in any order: (A) DI encapsulation — H2, (B) Input validation + dedup canonicalization — H3, H9, M3, (C) Settings hermeticity — H7, (D) Schema hardening — H5 + rename cleanup M11.

---

## Decisions

### Decision: ProcessResult carries converter_name and config_snapshot (H2)

**Chosen:** Add `converter_name: str` and `config_snapshot: dict[str, Any]` to `ProcessResult` in `core/orchestrator.py`.
The orchestrator populates both fields before returning.
The worker reads them directly from the result without making any additional method calls on the orchestrator.

**Rationale:** The orchestrator already resolves the converter to run the job; attaching the resolved name and snapshot to the return value costs one dict field assignment.
The alternative (letting the caller re-resolve) punches through encapsulation and couples the worker to the orchestrator's internal registry lookup.
Putting the data on the result also makes the worker's idempotency-key assembly fully self-contained — no second query path, no risk of the resolver returning a different instance on a hypothetical cache miss.

**Alternatives considered:**

- **Worker calls `orchestrator._resolve_converter` (current):** breaks encapsulation; private
  method semantics give no stability guarantee.
- **Worker stores converter_name separately before calling `process_with_provenance`:** then `config_snapshot` still requires a second resolver call after the fact.
  No benefit over putting both on the result.

---

### Decision: UrlRef.\_normalize import hoisted to module scope (H3)

**Chosen:** Move `from aizk.utilities.url_utils import normalize_url` to module scope in `core/source_ref.py`.
In the `_normalize` validator body, catch only `ValueError` and `validators.ValidationError`.
Fallback: `value.strip().casefold().rstrip("/")`.

**Rationale:** A lazy import inside a validator fires only on the first URL submission; an import error is silently swallowed and the fallback is used for every subsequent URL, causing unpredictable identity drift.
Hoisting to module scope makes the failure loud and immediate — the process refuses to start rather than running degraded.
Narrowing the catch prevents swallowing programmer errors (e.g., a `TypeError` from passing a non-string through) that indicate bugs, not expected normalization failures.
The fallback is made deterministic (`casefold + rstrip("/")`) so two environments with a broken normalizer produce the same hash rather than diverging on bare `strip()`.

**Alternatives considered:**

- **Hard-raise on normalization failure:** desirable for the submission path, but `TypeAdapter.validate_python` is also called during DB deserialization (`workers/orchestrator.py`), where the URL is already stored and a raise would fail job processing.
  The deterministic fallback preserves round-trip safety.
- **Keep lazy import, narrow catch only:** still silently degrades normalizer; import errors
  are still invisible.

---

### Decision: bookmark_id constrained via Field(pattern=...) (H9)

**Chosen:** `KarakeepBookmarkRef.bookmark_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")`.
`to_dedup_payload()` applies `.strip()` as belt-and-suspenders even though the pattern
already forbids whitespace.

**Rationale:** `bookmark_id` is embedded verbatim into outbound KaraKeep API URL paths; path traversal characters (`/`, `..`), NUL bytes, and non-ASCII would be silently accepted and could cause unexpected routing or DB uniqueness divergence on whitespace variants.
Pydantic-layer validation is the right enforcement point: it fires before any storage write or HTTP call.
The pattern matches what KaraKeep appears to use in practice (alphanumeric + `_-`).
Length cap of 64 is conservative; KaraKeep IDs are observed to be short UUIDs or slugs.

**Alternatives considered:**

- **Validate in the API route handler only:** leaves the core model weakly typed and allows
  test/fixture code to create invalid refs.
- **UUID-only constraint:** too restrictive; KaraKeep may use non-UUID IDs.

---

### Decision: Dedup canonicalization applies casefold to GitHub owner/repo (M3)

**Chosen:** `GithubReadmeRef.to_dedup_payload()` applies `.lower()` to `owner` and `repo`.
`KarakeepBookmarkRef.to_dedup_payload()` applies `.strip()` to `bookmark_id` (redundant with
the pattern constraint, kept as a guard).

**Rationale:** GitHub org and repo names are case-insensitive in the GitHub API; two callers using `"MyOrg/MyRepo"` and `"myorg/myrepo"` refer to the same content.
Without case normalization they produce different `source_ref_hash` values and different Source rows, causing duplicate work.
`owner` and `repo` are identity fields (in `to_dedup_payload()`), so casefolding them is a breaking change to the hash contract: a new Alembic revision is required if any `GithubReadmeRef` rows exist in production.
At cutover `GithubReadmeRef` is not submittable via `IngressPolicy`, so no rows of that kind exist; the change is breaking in principle but safe in practice at the cutover point.
The fixture-lock test is updated with the new expected hashes.

**Alternatives considered:**

- **Normalize at parse time via field_validator:** would normalize stored `source_ref` JSON as well, which is correct but changes the serialized form.
  Chosen `to_dedup_payload` normalization only keeps the stored ref readable in its original case while still deduplicating correctly.

---

### Decision: NOT NULL migration uses SQLite batch_alter_table (H5)

**Chosen:** A new Alembic migration using `op.batch_alter_table` (SQLite requires a full-table rebuild to enforce NOT NULL on an existing column).
A pre-flight `SELECT COUNT(*)` asserts zero NULLs and raises `IrreversibleMigrationError` if any exist.
The `Source` SQLModel definition changes `str | None` → `str` and removes `nullable=True` for both columns.
The existing `model_validator` (added in the prior hotfix) is retained as defense-in-depth.

**Rationale:** SQLite does not support `ALTER COLUMN … NOT NULL` directly; `batch_alter_table` is the idiomatic Alembic approach.
The pre-flight assertion means the migration never partially succeeds — it either runs cleanly on a healthy database or aborts before touching anything, preserving the conditional-reversibility contract established by the `bookmarks → sources` migration.

**Alternatives considered:**

- **No migration; rely on model_validator:** leaves the DB schema mismatched with the ORM model;
  the existing `test_migrations.py` equivalence test would fail CI.
- **Add NOT NULL with a default value:** `source_ref` and `source_ref_hash` have no meaningful
  default; a sentinel default would be semantically incorrect.

---

### Decision: Settings instances constructed once at composition root (H7)

**Chosen:**

1. All `BaseSettings` subclasses in `utilities/config.py` and `wiring/ingress_policy.py`
   change their `model_config` to `SettingsConfigDict(..., env_file=None)`.
2. `build_api_runtime` and `build_worker_runtime` call `dotenv.load_dotenv()` explicitly at
   the top (already present in wiring; move before settings construction if not there).
3. `build_api_runtime` constructs `DoclingConverterConfig` once and stores it on `app.state`
   (e.g., `app.state.docling_config`).
4. `api/routes/jobs.py` and `api/routes/health.py` read from `request.app.state.docling_config`
   instead of calling `DoclingConverterConfig()`.
5. `cli.py` constructs `DoclingConverterConfig` once at the top of each CLI command rather
   than inside loops.

**Rationale:** `DoclingConverterConfig()` with `env_file=".env"` reads from disk on every instantiation.
In production this means each job submission and each k8s readiness probe re-reads `.env`.
If `.env` ever mutates at runtime, the idempotency key formula silently changes for the same input, violating the idempotency guarantee.
In tests, every instantiation can silently pick up developer-machine settings, violating the hermeticity contract.
Constructing once at the composition root and routing through `app.state` fixes both: one read, stable across the process lifetime, and testable by injecting a custom instance before lifespan runs.

**Alternatives considered:**

- **`@lru_cache` on `DoclingConverterConfig()`:** caches the instance globally, which is
  correct for production but not overridable in tests without `cache_clear()`; less explicit
  than `app.state`.
- **`env_file=None` only for test callers:** was the prior approach (requiring `_env_file=None`
  in tests); does not protect production paths.
