# Delta for pluggable-pipeline

## MODIFIED Requirements

### Requirement: Compute source_ref_hash from a canonical dedup payload

The system SHALL compute `source_ref_hash` by invoking each `SourceRef` variant's `to_dedup_payload()` method to obtain a canonical, normalized dict containing only the fields that define dedup sameness for that variant, then hashing the JSON-encoded payload with stable key ordering and separators.
The hash SHALL NOT be derived from `model_dump_json()` of the full ref, so cosmetic changes (field ordering, default values, non-sameness fields) do not produce different hashes for the same logical source.

The canonical dedup payload constitutes a versioned dedup-key contract.
Any change that alters the `to_dedup_payload()` output for previously-accepted refs (key rename, normalization rule change, field added to or removed from the payload) SHALL be treated as a breaking change to the Source **dedup key** (`source_ref_hash`) — the content fingerprint that determines which submissions resolve to the same `source_id`, not the durable identity itself — and SHALL be accompanied by a data migration that recomputes `source_ref_hash` for affected rows.
Additive `SourceRef` fields that do not participate in `to_dedup_payload()` are not breaking.
A fixture-lock test SHALL pin a curated set of `(variant_instance, expected_sha256)` pairs so accidental drift in `to_dedup_payload()` output (e.g., whitespace, key ordering, normalization) fails CI before shipping.

All `SourceRef` variants' `to_dedup_payload()` implementations SHALL apply consistent normalization to string sameness-key fields before including them in the payload:

- String fields used in the sameness key SHALL have leading/trailing whitespace stripped.
- Fields on case-insensitive external namespaces SHALL be casefolded.
  Specifically: `GithubReadmeRef.owner` and `GithubReadmeRef.repo` SHALL be lowercased (GitHub org/repo names are case-insensitive); `KarakeepBookmarkRef.bookmark_id` SHALL have whitespace stripped (pattern constraint prevents embedded whitespace; strip guards against edge cases at the boundary).

A fixture-lock test SHALL pin one normalization-sensitive instance per variant (e.g., a `GithubReadmeRef` with mixed-case `owner`) to confirm that casefolding is applied before hashing.

Serves: coherent-pipeline-foundation

> Previously: `source_ref_hash` was described as "Source identity" and its contract as a "versioned identity contract"; it is the source **dedup/sameness key** (a content fingerprint), distinct from the durable surrogate identity `source_id`. No token is renamed — the prose is corrected to stop calling a hash an identity.

#### Scenario: Dedup payload fixture-lock guards against accidental drift

- **GIVEN** a curated fixture of `SourceRef` instances with their expected `source_ref_hash` values (one per variant, each including at least one non-trivial normalization case)
- **WHEN** `compute_source_ref_hash` is run against each fixture instance
- **THEN** the computed hash equals the pinned expected hash; a change to `to_dedup_payload()` that alters any output fails this test and signals a breaking dedup-key change requiring a migration

#### Scenario: Equivalent refs produce identical hash

- **GIVEN** two `ArxivRef` instances with the same `arxiv_id` but differing cosmetic fields (e.g., `arxiv_pdf_url` present vs. absent)
- **WHEN** `source_ref_hash` is computed for each
- **THEN** the hashes are identical

#### Scenario: Sameness-defining field differs

- **GIVEN** two `ArxivRef` instances with different `arxiv_id` values
- **WHEN** `source_ref_hash` is computed for each
- **THEN** the hashes differ

#### Scenario: InlineHtmlRef hash is content-addressed

- **GIVEN** two `InlineHtmlRef` instances with identical body bytes
- **WHEN** `source_ref_hash` is computed for each
- **THEN** the hashes are identical (payload hashes the body content, not metadata)

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

### Requirement: Represent content sources as a discriminated union

The system SHALL represent the source of content to be fetched as a `SourceRef` — a pydantic discriminated union keyed on a `kind` field.
Each variant SHALL carry only the data needed to fetch its content and SHALL be serializable to and from JSON for persistence.
Each variant SHALL expose a `to_dedup_payload() -> dict` method returning a canonical, normalized representation used for dedup-key hashing (see "Compute source_ref_hash from a canonical dedup payload").
Exception: `InlineHtmlRef` MAY embed content bytes directly, subject to a hard size cap of 64 KiB measured on the raw body bytes (not the serialized JSON form), as a documented exception for small inline-text payloads.
Serialized-JSON bloat from escaping is accepted; typical expansion is ~1.3× for HTML-shaped content.

Variants MAY carry cosmetic or forward-compatibility fields that are excluded from `to_dedup_payload()` and MAY also be ignored by the current fetcher implementation.
At cutover the following fields are accepted at deserialization but intentionally non-load-bearing:

- `ArxivRef.arxiv_pdf_url` — cosmetic fetcher hint preserved for observability; does not affect fetch behavior or the dedup key.
- `GithubReadmeRef.branch` — accepted for forward compatibility but currently ignored by `GithubReadmeFetcher`, which hardcodes a `main`/`master` fallback.
  Wiring branch through to the fetcher is deferred until `IngressPolicy` widens to admit `github_readme` for public submission.

Accepted-but-ignored fields SHALL be documented both on the model (docstring) and in this specification so that consumers are not misled into believing the field is load-bearing at cutover.

`KarakeepBookmarkRef.bookmark_id` SHALL satisfy the constraint `^[A-Za-z0-9_-]{1,64}$`, validated at Pydantic construction time.
Values that do not match SHALL be rejected with a validation error before any downstream processing.

`UrlRef` SHALL validate input URLs with the project's canonical URL normalizer; import failures SHALL surface at module-load time, not deferred to first use.
The `_normalize` validator SHALL catch only the documented error types of the URL normalizer (`ValueError`, `validators.ValidationError`); it SHALL NOT use bare `except Exception`.
When the normalizer raises a recognized error, the fallback SHALL apply deterministic normalization (`strip` + `casefold` + `rstrip("/")`) rather than bare `strip()`, so the fallback output is stable across environments.

Serves: coherent-pipeline-foundation

> Previously: `to_dedup_payload()` output was described as being "used for identity hashing", and excluded fields were said not to "affect identity"; corrected to name it the dedup key (a content fingerprint), distinct from the durable surrogate identity `source_id`. No token is renamed.

#### Scenario: Accepted-but-ignored field round-trips without affecting the dedup key

- **GIVEN** two `GithubReadmeRef` instances with identical `owner` and `repo` but different `branch` values (one `"main"`, one `"develop"`)
- **WHEN** `source_ref_hash` is computed for each
- **THEN** the hashes are identical because `branch` is excluded from `to_dedup_payload()`

#### Scenario: SourceRef round-trips through JSON

- **GIVEN** a `SourceRef` variant (e.g., `ArxivRef(arxiv_id="2301.12345")`)
- **WHEN** it is serialized to JSON and deserialized back
- **THEN** the deserialized value equals the original, with the correct variant type restored via the `kind` discriminator

#### Scenario: Unknown kind rejected on deserialization

- **GIVEN** a JSON object with a `kind` value not matching any registered variant
- **WHEN** deserialization is attempted
- **THEN** a validation error is raised

#### Scenario: InlineHtmlRef exceeding size cap rejected

- **GIVEN** an `InlineHtmlRef` whose raw body bytes exceed 64 KiB
- **WHEN** the model is constructed
- **THEN** a pydantic validation error is raised (the check is against raw body length, not serialized JSON length)

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
