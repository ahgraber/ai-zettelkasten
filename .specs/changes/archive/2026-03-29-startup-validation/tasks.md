# Tasks: Startup Validation

## Startup validation module

- [x] Create `src/aizk/conversion/utilities/startup.py` with `validate_startup(config, role)` that runs required service probes and logs the feature summary
- [x] Implement `probe_s3(config)` — HEAD bucket with 10s timeout; raise on failure
- [x] Implement `probe_karakeep()` — GET bookmarks?limit=1 with API key header and 10s timeout; raise on failure
- [x] Implement `log_feature_summary(config, role)` — structured INFO log entry listing enabled/disabled status for picture descriptions, MLflow tracing, and Litestream replication with reasons

## CLI integration

- [x] Wire `validate_startup()` into `_cmd_worker()` in `cli.py` after config load, before MLflow/Litestream/migrations
- [x] Wire `validate_startup()` into `_cmd_serve()` in `cli.py` after config load, before MLflow/Litestream/uvicorn
- [x] Remove `_require_karakeep_env()` calls — superseded by the KaraKeep reachability probe

## Tests

- [x] Test `probe_s3` succeeds with reachable mock S3
- [x] Test `probe_s3` raises with unreachable/invalid S3
- [x] Test `probe_karakeep` succeeds with reachable mock KaraKeep
- [x] Test `probe_karakeep` raises with unreachable KaraKeep
- [x] Test `log_feature_summary` logs correct enabled/disabled states for all feature combinations
- [x] Test `validate_startup` calls probes in order and raises on first failure
