# Rebuilding the database

This procedure replaces the conversion database (which the graph stages share) with a new, empty one.
No data crosses from the old database to the new one; the corpus is rebuilt by re-ingestion.

## When to use it

Use it when a service refuses to start with `LegacyDatabaseError`.
The migration runner raises that error for a database stamped with a revision the tree does not know, or for a database that has tables but no migration stamp.
Every database created before `0.2.0` is refused, because the migration tree holds a single baseline revision and no chain to migrate from.

## Cost and consequences

- **Re-ingestion reconverts every source.**
  Each source gets a new `source_id`, so its conversion runs again, with the Docling and picture-description spend that implies, and so do contextualization and extraction.
- **Old artifacts become unreferenced.**
  Conversion artifacts live under `s3://<bucket>/<source_id>/`.
  New `source_id` values mean new prefixes, so every old prefix is left without a database row that points to it.
  Keep the old prefixes until you accept the new corpus: they are part of the rollback.
  Plan object storage for two full corpora until then.
- **Work in flight in the old database is not carried over.**
  Jobs that had not succeeded are submitted again by the re-ingestion.
- **The console fails against an old database.**
  A console pointed at a database that was not rebuilt answers with HTTP 500; it has no degraded mode.

## Procedure

1. **Stop the workers and wait for them to drain.**
   Stop `aizk-conversion worker`, `aizk-graph worker`, and `aizk-graph extraction-worker`, and wait until each process exits.
   Then stop `aizk-conversion serve` and `aizk-graph serve`.
2. **Archive the old database.**
   Move the database file out of its path, together with its `-wal` and `-shm` files.
   Keep with it the running configuration, the installed version, and the current Litestream replica prefix (`AIZK_LITESTREAM_S3_PREFIX`, default `db`).
   This archive and the old S3 prefixes are the rollback path.
3. **Verify the archive.**
   Open the archived copy and run `PRAGMA integrity_check;` against it.
   Do not continue until it reports `ok`.
4. **Point Litestream at a new replica prefix.**
   Set `AIZK_LITESTREAM_S3_PREFIX` to a prefix that was never used, for example `db-0.2.0`.
   When a service starts and finds no database file, it restores one from the replica if `AIZK_LITESTREAM_RESTORE_ON_STARTUP` is `true` (the default).
   With the old prefix, that restore brings back the old database, and the migration runner refuses it again.
   The old prefix stays untouched as part of the rollback.
5. **Create the new database.**
   Run `just db-init` (`uv run aizk-conversion db-init`).
   A service started without this step runs the same migration runner and creates the database itself.
6. **Start the new version.**
   Start `aizk-conversion serve`, `aizk-graph serve`, and the three workers.
7. **Re-ingest.**
   Run the backfills described in the README, section "Backfill".
   Start with `just backfill conversion --yes`, and run `just backfill` again as the workers drain; each pass picks up what the previous one made eligible.
   Pace the re-ingestion to the inference and conversion spend you accept.
8. **Smoke-check on the operator console.**
   Ingest a few inputs and confirm, at `http://<host>:8001/ui`:
   - each input propagates from conversion through contextualization and extraction;
   - each unit's drill-down shows its creation event;
   - the dashboard's pending and stale counts are exact;
   - the content explorer finds the new content.
9. **Accept the new corpus, then reclaim storage.**
   When you accept the new corpus, delete the archived database and the old Litestream replica prefix.
   The old conversion artifacts are every `s3://<bucket>/<source_id>/` prefix whose `source_id` is not in the new database's `sources` table; delete those prefixes.

## Rollback

Rollback is possible until step 9.

1. Stop every service.
2. Put the archived database file, with its `-wal` and `-shm` files, back at its path.
3. Restore the archived configuration, including the old `AIZK_LITESTREAM_S3_PREFIX`.
4. Start the previous version.

The old S3 prefixes are unchanged, so the old database finds every artifact it references.
