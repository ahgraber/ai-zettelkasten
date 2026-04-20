# Expected Schema Changes: pluggable-fetch-convert-hardening

## Database: `sources` table — NOT NULL enforcement

Two columns that were left nullable at the `pluggable-fetch-convert` cutover are tightened:

- **`source_ref TEXT`** → **`source_ref TEXT NOT NULL`**
- **`source_ref_hash TEXT`** → **`source_ref_hash TEXT NOT NULL`**

Every row written by the post-cutover API already has both columns populated; the migration
asserts this before altering schema and raises `IrreversibleMigrationError` if any NULL is
found, so the operation is safe on a healthy database and aborts loudly on a partially-backfilled
one.

The `UNIQUE INDEX ix_sources_source_ref_hash` is unaffected (unique index on a NOT NULL column
behaves identically in SQLite).

## No OpenAPI change

The OpenAPI surface is unchanged by this change.
`source_ref` and `source_ref_hash` are internal columns never surfaced in API response bodies
as nullable vs. non-nullable markers.

`bookmark_id` validation (H9) tightens the accepted input at the Pydantic layer; the OpenAPI schema for `KarakeepBookmarkRef` gains a `pattern` constraint on `bookmark_id`.
The before snapshot reflects the current pattern-free schema; the after snapshot (generated at verify time) will show the added `pattern` field.
