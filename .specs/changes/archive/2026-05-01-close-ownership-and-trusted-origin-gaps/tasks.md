# Tasks: close-ownership-and-trusted-origin-gaps

## Output ownership enforcement

- [x] Update `src/aizk/conversion/api/routes/bookmarks.py` so
  `GET /v1/bookmarks/{aizk_uuid}/outputs` resolves `principal` and filters by
  `ConversionOutput.owner_id == principal.subject`
- [x] Update `src/aizk/conversion/api/routes/outputs.py` so manifest, markdown, and
  figure routes return the not-found shape when `output.owner_id != principal.subject`
- [x] Add regression coverage in
  `tests/conversion/integration/test_bookmark_outputs.py` and
  `tests/conversion/integration/test_output_content.py` for:
  same-owner success, cross-owner empty output list, and cross-owner 404 artifact
  access

## Owner-scoped idempotency

- [x] Update `src/aizk/conversion/api/routes/jobs.py` so duplicate-submission lookup
  uses `(owner_id, idempotency_key)` rather than `idempotency_key` alone
- [x] Update `src/aizk/conversion/datamodel/job.py` and the supporting migration so
  database uniqueness is owner-scoped and remains safe under concurrent submits
- [x] Add migration coverage in `tests/conversion/integration/test_migrations.py` for
  the new uniqueness/index shape and downgrade behavior
- [x] Add API/contract coverage in
  `tests/conversion/contract/test_jobs_idempotency.py` for:
  same-owner replay returns the existing job, different-owner replay creates a new
  job while reusing the same Source row

## Exact KaraKeep trusted-origin matching

- [x] Add a shared helper for exact KaraKeep trusted-asset matching and use it from
  both `src/aizk/conversion/adapters/fetchers/url.py` and
  `src/aizk/conversion/adapters/fetchers/arxiv.py`
- [x] Require exact `(scheme, hostname, port)` equality plus a path under
  `/api/v1/assets/`; remove string-prefix trust checks
- [x] Add unit coverage for:
  exact-origin trusted asset URL, lookalike prefix/suffix host rejection, and
  same-origin non-asset URL falling back to the normal egress path

## Verification

- [x] Run targeted tests with `uv run pytest tests/conversion/contract/test_jobs_idempotency.py tests/conversion/integration/test_bookmark_outputs.py tests/conversion/integration/test_output_content.py tests/conversion/integration/test_migrations.py`
- [x] If sandbox limits block the test run, hand the exact command to the user and
  note the gap in the completion summary
