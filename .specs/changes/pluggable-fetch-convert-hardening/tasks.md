# Tasks: pluggable-fetch-convert-hardening

Tasks are grouped by cluster so each cluster can be implemented and reviewed independently.
Within a cluster, order is dependency-first.

---

## Cluster A — DI encapsulation (H2)

- [x] `core/orchestrator.py`: add `converter_name: str` and `config_snapshot: dict[str, Any]`
  fields to `ProcessResult`; update `process_with_provenance` to populate both fields from the
  resolved converter before returning.
- [x] `workers/orchestrator.py`: remove the `runtime.orchestrator._resolve_converter(...)` call
  and `hasattr(converter, "config_snapshot")` guard; read `result.converter_name` and
  `result.config_snapshot` directly from `ProcessResult`.
- [x] Tests: add unit test asserting that `ProcessResult` from a fake-orchestrator run carries
  correct `converter_name` and `config_snapshot`; assert no call to `_resolve_converter` is made
  by the worker.

---

## Cluster B — Input validation + dedup canonicalization (H3, H9, M3)

- [x] `core/source_ref.py`: hoist `from aizk.utilities.url_utils import normalize_url` to
  module scope (not inside the validator body).
- [x] `core/source_ref.py` — `UrlRef._normalize`: replace bare `except Exception` (on the
  import and on the call) with `except (ValueError, validators.ValidationError)`; change the
  fallback to `stripped.casefold().rstrip("/")`.
- [x] `core/source_ref.py` — `KarakeepBookmarkRef.bookmark_id`: add
  `Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")`.
- [x] `core/source_ref.py` — `KarakeepBookmarkRef.to_dedup_payload`: apply `.strip()` to
  `bookmark_id` in the returned dict.
- [x] `core/source_ref.py` — `GithubReadmeRef.to_dedup_payload`: apply `.lower()` to `owner`
  and `repo` in the returned dict.
- [x] `tests/conversion/unit/core/test_source_ref.py`: update the fixture-lock expected hashes
  for `GithubReadmeRef` (casefolding now applies) and add one normalization-sensitive fixture
  instance per variant (e.g., `GithubReadmeRef(owner="MyOrg", repo="MyRepo")` hashes the
  same as `GithubReadmeRef(owner="myorg", repo="myrepo")`).
- [x] `tests/conversion/unit/core/test_source_ref.py`: add property-based test (hypothesis)
  asserting `compute_source_ref_hash` is stable under leading/trailing whitespace variation for
  `KarakeepBookmarkRef.bookmark_id` (values within the allowed pattern) and under case variation
  for `GithubReadmeRef.owner`/`repo`.
- [x] `tests/conversion/unit/core/test_source_ref.py`: add test asserting that
  `KarakeepBookmarkRef(bookmark_id=" bad id")` raises `ValidationError` (whitespace violates
  pattern); add test for oversized ID (>64 chars).

---

## Cluster C — Settings hermeticity (H7)

- [ ] `utilities/config.py`: change every `BaseSettings` subclass `model_config` to use
  `env_file=None` as the default (remove `env_file=".env"` where present or absent-and-
  implicit); confirm `dotenv.load_dotenv()` is called at the composition root before any
  settings construction.
- [ ] `wiring/ingress_policy.py`: change `IngressPolicy.model_config` to `env_file=None`.
- [ ] `wiring/api.py` (`build_api_runtime`): construct `DoclingConverterConfig` once inside the
  builder; attach as `app.state.docling_config`.
- [ ] `wiring/worker.py` (`build_worker_runtime`): construct `DoclingConverterConfig` once
  inside the builder; pass it directly to the orchestrator or store where the worker loop reads
  it.
- [ ] `api/routes/jobs.py`: replace `DoclingConverterConfig()` call with
  `request.app.state.docling_config`.
- [ ] `api/routes/health.py`: replace `DoclingConverterConfig()` call with
  `request.app.state.docling_config`.
- [ ] `cli.py`: replace per-command `DoclingConverterConfig()` calls with a single instance
  constructed at the top of each command function (before any branching).
- [ ] Tests: add test asserting that `DoclingConverterConfig()` constructed with no arguments
  (default `env_file=None`) does not read a `.env` file from disk; verify the existing app
  fixture does not call `DoclingConverterConfig()` after `app.state.config` is set.

---

## Cluster D — Schema hardening + rename cleanup (H5, M11)

- [ ] `datamodel/source.py`: change `source_ref: str | None` → `source_ref: str`; change
  `source_ref_hash: str | None` → `source_ref_hash: str`; remove `nullable=True` from both
  `sa_column` definitions; retain the existing `model_validator` as defense-in-depth.
- [ ] New Alembic migration `<hash>_enforce_source_ref_not_null.py`:
  - `upgrade()`: count rows where `source_ref IS NULL OR source_ref_hash IS NULL`; raise
    `IrreversibleMigrationError` if count > 0; use `op.batch_alter_table("sources")` to
    rebuild both columns as NOT NULL.
  - `downgrade()`: use `op.batch_alter_table("sources")` to rebuild both columns back to
    nullable.
- [ ] `tests/conversion/unit/test_migrations.py`: add scenario verifying the pre-flight abort
  (insert a row with NULL `source_ref`, assert `IrreversibleMigrationError` on upgrade);
  add round-trip test on a fully-populated database; verify ORM-baseline equivalence holds
  after the new migration (nullability of both columns matches `SQLModel.metadata.create_all()`).
- [ ] Rename cleanup — `api/routes/jobs.py`: remove `from ... import Source as Bookmark`;
  import `Source` directly; rename all `bookmark` local variables to `source`.
- [ ] Rename cleanup — `api/routes/ui.py`: remove `from ... import Source as Bookmark`;
  import `Source` directly; rename `Bookmark` usages to `Source`.
- [ ] Rename cleanup — `workers/uploader.py`: remove `from ... import Source as BookmarkRecord`;
  import `Source` directly; rename `bookmark_record` / `BookmarkRecord` usages to `source` /
  `Source`.
