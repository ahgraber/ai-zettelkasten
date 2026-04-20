# Delta for pluggable-pipeline

## MODIFIED Requirements

### Requirement: Inject fetcher and converter resolution into the orchestrator

The orchestrator's result type SHALL carry the converter name and its config snapshot so that callers (e.g., the worker) do not need to re-resolve the converter to obtain those values.
Specifically, `ProcessResult` SHALL include `converter_name: str` and `config_snapshot: dict[str, Any]`, written by the orchestrator before returning to the caller.
No caller SHALL access `Orchestrator._resolve_converter` directly; the private resolver method is an implementation detail of the orchestrator and SHALL NOT be reached from outside the orchestrator class. (Previously: `ProcessResult` did not carry converter metadata; workers re-resolved the converter via the private `_resolve_converter` method to obtain `config_snapshot`.)

#### Scenario: ProcessResult carries converter metadata

- **GIVEN** an orchestrator that successfully completes the fetch-convert cycle
- **WHEN** the caller receives the `ProcessResult`
- **THEN** `result.converter_name` and `result.config_snapshot` are populated with the converter
  that ran and its output-affecting configuration snapshot, without the caller making any
  additional resolver calls

#### Scenario: Worker consumes config_snapshot from ProcessResult directly

- **GIVEN** a worker that receives a `ProcessResult` from the orchestrator
- **WHEN** the worker constructs the idempotency key or records provenance
- **THEN** it reads `config_snapshot` from the result without calling any orchestrator method
  beyond `process_with_provenance`

---

### Requirement: Represent content sources as a discriminated union

`KarakeepBookmarkRef.bookmark_id` SHALL satisfy the constraint `^[A-Za-z0-9_-]{1,64}$`, validated at Pydantic construction time.
Values that do not match SHALL be rejected with a validation error before any downstream processing. (Previously: `bookmark_id` was an unconstrained `str` field with no pattern or length check.)

`UrlRef` SHALL validate input URLs with the project's canonical URL normalizer; import failures SHALL surface at module-load time, not deferred to first use.
The `_normalize` validator SHALL catch only the documented error types of the URL normalizer (`ValueError`, `validators.ValidationError`); it SHALL NOT use bare `except Exception`.
When the normalizer raises a recognized error, the fallback SHALL apply deterministic normalization (`strip` + `casefold` + `rstrip("/")`) rather than bare `strip()`, so the fallback output is stable across environments. (Previously: the `normalize_url` import was lazy inside the validator body; both the import and the normalizer call were wrapped in bare `except Exception`, meaning import errors were silently swallowed at first call rather than surfacing at startup.)

#### Scenario: Invalid bookmark_id rejected at construction

- **GIVEN** a `KarakeepBookmarkRef` construction with a `bookmark_id` value that contains
  spaces, non-ASCII characters, or exceeds 64 characters
- **WHEN** the model is constructed
- **THEN** a pydantic `ValidationError` is raised before the ref reaches any storage or
  HTTP call-site

#### Scenario: normalize_url import failure surfaces at startup

- **GIVEN** a process where `aizk.utilities.url_utils` cannot be imported (missing dependency)
- **WHEN** the `aizk.conversion.core.source_ref` module is imported
- **THEN** the import fails immediately, not silently at the first URL submission

#### Scenario: UrlRef normalizer exception falls back deterministically

- **GIVEN** a URL string that `normalize_url` raises `ValueError` for
- **WHEN** `UrlRef` is constructed with that string
- **THEN** the stored value is `url.strip().casefold().rstrip("/")`, identical across
  environments and distinct from bare `url.strip()`

---

### Requirement: Compute source_ref_hash from a canonical dedup payload

All `SourceRef` variants' `to_dedup_payload()` implementations SHALL apply consistent
normalization to string identity fields before including them in the payload:

- String fields used as identity SHALL have leading/trailing whitespace stripped.
- Fields on case-insensitive external namespaces SHALL be casefolded.
  Specifically: `GithubReadmeRef.owner` and `GithubReadmeRef.repo` SHALL be lowercased (GitHub org/repo names are case-insensitive); `KarakeepBookmarkRef.bookmark_id` SHALL have whitespace stripped (pattern constraint prevents embedded whitespace; strip guards against edge cases at the boundary).

(Previously: `KarakeepBookmarkRef.to_dedup_payload()` did not strip `bookmark_id`;
`GithubReadmeRef.to_dedup_payload()` did not casefold `owner` or `repo`.)

A fixture-lock test SHALL pin one normalization-sensitive instance per variant (e.g., a
`GithubReadmeRef` with mixed-case `owner`) to confirm that casefolding is applied before
hashing.

#### Scenario: Mixed-case GitHub owner/repo produces the same hash as lowercased

- **GIVEN** `GithubReadmeRef(owner="MyOrg", repo="MyRepo")` and
  `GithubReadmeRef(owner="myorg", repo="myrepo")`
- **WHEN** `compute_source_ref_hash` is run against each
- **THEN** the hashes are identical

#### Scenario: Bookmark ID with leading/trailing whitespace normalizes to same hash

- **GIVEN** `KarakeepBookmarkRef(bookmark_id="abc123")` and an attempt to construct
  `KarakeepBookmarkRef(bookmark_id=" abc123")` (leading space)
- **WHEN** the second construction is attempted
- **THEN** a pydantic `ValidationError` is raised (pattern forbids whitespace), so two
  callers cannot produce divergent hashes for the logically same bookmark

---

### Requirement: Wire adapters via role-specific builders

Role-specific builder functions SHALL construct all `BaseSettings` instances exactly once and SHALL NOT allow settings to be re-read from disk or environment on subsequent calls.
Every `BaseSettings` subclass in the conversion package SHALL default `env_file=None` in its `model_config`; the composition root (builder functions) is the only permitted site that loads `.env` via `python-dotenv` before constructing settings.
Settings instances that are needed at request time (e.g., `DoclingConverterConfig`) SHALL be attached to `app.state` by the builder function and read via `request.app.state` by request handlers; they SHALL NOT be re-instantiated per request or per health probe. (Previously: `DoclingConverterConfig()` was instantiated inside request handlers and health probes, re-reading `.env` on every call; `IngressPolicy` had `env_file=".env"` as its default, allowing drift when `.env` mutated at runtime.)

#### Scenario: DoclingConverterConfig is constructed once per process

- **GIVEN** an API process that handles multiple job-submission requests
- **WHEN** each request is processed
- **THEN** the `DoclingConverterConfig` instance used for idempotency-key computation is the
  same object constructed at startup, not a new instance per request

#### Scenario: BaseSettings subclass does not read .env by default

- **GIVEN** a `DoclingConverterConfig` or `IngressPolicy` constructed with no explicit
  `_env_file` argument
- **WHEN** the instance is created
- **THEN** no `.env` file on disk is read; field values come only from environment variables
  already present in the process or from explicit constructor arguments
