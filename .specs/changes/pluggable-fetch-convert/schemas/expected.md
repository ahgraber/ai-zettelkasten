# Expected Schema Changes: pluggable-fetch-convert

## Database: `bookmarks` table renamed to `sources`

- Table renamed from `bookmarks` to `sources`.
- **`karakeep_id`** becomes nullable (was required).
  Only populated for KaraKeep-backed sources.
- **New column** `source_ref` (JSON): the canonical `SourceRef` fetch instruction, stored as a JSON object with a `kind` discriminator.
- **New column** `source_ref_hash` (text, unique index): structural hash of the serialized `SourceRef`, used for dedup.
- **Existing columns retained**: `aizk_uuid` (unique, unchanged), `url`, `normalized_url`, `title`, `content_type`, `source_type`, timestamps.
- **`source_type`** retained as derived metadata (resolved semantic origin, distinct from `source_ref.kind`).
- **Backfill**: existing rows receive a `KarakeepBookmarkRef` as `source_ref` and a computed `source_ref_hash`.

## Database: `conversion_jobs` table

- **New column** `source_ref` (JSON): denormalized copy of the Source row's `source_ref` for fetch-chain use.
- **FK unchanged**: `aizk_uuid` FK continues to reference `sources.aizk_uuid` (renamed from `bookmarks.aizk_uuid`).

## `POST /v1/jobs` — Request body (`JobSubmission`)

- **`karakeep_id` field removed.**
  Replaced by a required `source_ref` field.
- **New required field** `source_ref`: a discriminated union (discriminator: `kind`) accepting one of `KarakeepBookmarkRef`, `ArxivRef`, `GithubReadmeRef`, `UrlRef`, `SingleFileRef`, `InlineHtmlRef`.
- KaraKeep callers submit `{"source_ref": {"kind": "karakeep_bookmark", "bookmark_id": "..."}}` instead of `{"karakeep_id": "..."}`.
- **Kind gating**: the API validates `source_ref.kind` against the set of kinds whose fetcher adapters are registered in the current deployment.
  Unregistered kinds return HTTP 422.

## `JobResponse` — Response schema

- **`karakeep_id` retained as a nullable compatibility field** (`str | None`).
  Populated when `source_ref.kind == "karakeep_bookmark"`; null otherwise.
  Retention preserves the UI contract; a UI migration to `source_ref` is deferred to a later change.
- **New field** `source_ref`: the stored `SourceRef` value for the job (always present).
- Existing fields `url: AnyUrl | None` and `title: str | None` retain their current names and semantics — populated when available, null otherwise.
  **Note:** the field is `url`, not `bookmark_url`.

## `JobList` — Query parameters

- `karakeep_id` filter removed. `aizk_uuid` filter remains.
- Filtering by `source_ref.kind` is deferred to a later change.

## New schema components

- `SourceRef` discriminated union schema (oneOf with `kind` discriminator).
- Individual ref schemas: `KarakeepBookmarkRef`, `ArxivRef`, `GithubReadmeRef`, `UrlRef`, `SingleFileRef`, `InlineHtmlRef`.

## Manifest (`manifest.json` on S3)

- **`version` bumps from `"1.0"` to `"2.0"`.**
  Writers produce v2.0 only after the cutover; readers are implemented as version-specific classes (`ManifestV1`, `ManifestV2`), both with `model_config = ConfigDict(extra="forbid")`.
  A version-dispatching loader selects the reader class from the serialized `version` string.
- **`ManifestSource` fields become nullable**: `url`, `normalized_url`, `title`, `source_type`, `fetched_at` are now `str | None` (required in v1.0).
  Non-KaraKeep jobs whose fetcher chain did not populate a title or URL can now serialize.
- **`karakeep_id` relocates**: removed from the top-level `ManifestSource` block.
  It now appears in either the `provenance` block or the `ingress` block depending on where the fetcher chain terminates.
- **New typed `provenance` block** describes the **terminal fetch state** — the identifiers the fetcher chain ultimately resolved to — keyed on the terminal ref's kind.
  Variants: `karakeep_bookmark` (carrying `bookmark_id`), `url` (carrying `url`), `arxiv` (carrying `arxiv_id`), `github_readme` (carrying `owner`, `repo`), and `inline_html` (carrying `content_hash`).
- **New optional `ingress` block** carries the submitter-supplied `SourceRef` **only when it differs from the terminal provenance** (e.g., a KaraKeep bookmark that resolved to an `ArxivRef` records `ingress.kind == "karakeep_bookmark"` with the original `bookmark_id` preserved alongside `provenance.kind == "arxiv"`).
  When ingress and terminal ref are the same kind with the same identifiers, the `ingress` block is omitted.
- **`config_snapshot` gains `converter_name: str`** alongside the converter-adapter-supplied fields.
  The `ManifestConfigSnapshotV2` model sets `extra="forbid"` so unknown fields fail at read time.
  Docling-specific field names remain under the Docling snapshot contribution; the orchestrator treats adapter snapshots as opaque.

## Unchanged

- All health endpoints (`/health/live`, `/health/ready`).
- All output endpoints (`/v1/outputs/*`, `/v1/bookmarks/*/outputs`).
- Bulk actions endpoint (`/v1/jobs/actions`).
- Error response shapes.
- S3 output layout (paths, file names, byte content of produced artifacts).

## Configuration (env vars)

- **Removed**: `AIZK_DOCLING_*` flat namespace (no compatibility shim).
- **Added**: `AIZK_CONVERTER__DOCLING__*` nested namespace for converter config.
- **Added**: Per-fetcher namespaces (e.g., `AIZK_FETCHER__KARAKEEP__*`).
