# Tasks: Job Visibility Permissions

## Wire principal dependency to read routes

- [x] Add `principal: Annotated[Principal, Depends(get_principal)]` parameter to
  `list_jobs` in `src/aizk/conversion/api/routes/jobs.py`
- [x] Add `principal: Annotated[Principal, Depends(get_principal)]` parameter to
  `get_job` in `src/aizk/conversion/api/routes/jobs.py`
- [x] Add `principal: Annotated[Principal, Depends(get_principal)]` parameter to
  `get_job_status_counts` in `src/aizk/conversion/api/routes/jobs.py`

## Owner-scope list and count queries

- [x] Add `.where(ConversionJob.owner_id == principal.subject)` to the `query` and
  `count_query` statements in `list_jobs` (alongside existing filters)
- [x] Add `.where(ConversionJob.owner_id == principal.subject)` to the count query
  in `get_job_status_counts`

## Owner check on get and mutation routes

- [x] In `get_job`: after the 404-on-missing check, add
  `if job.owner_id != principal.subject: raise HTTPException(404, {"error": "job_not_found", ...})`
- [x] In `retry_job`: after the 404-on-missing check, add the same owner check;
  remove the `# noqa: ARG001` comment
- [x] In `cancel_job`: after the 404-on-missing check, add the same owner check;
  remove the `# noqa: ARG001` comment
- [x] In `bulk_job_actions`: inside the per-job loop, after the not-found check,
  add an owner check that appends `BulkActionResult(job_id=job_id, status="error", error="job_not_found")`
  and increments `errors` on mismatch; remove the `# noqa: ARG001` comment

## Tests

Group ownership-scenario tests in a single new file `tests/conversion/integration/test_jobs_ownership.py`:

- [x] List: assert a job with `owner_id != "self"` is absent from `GET /v1/jobs` results
  and excluded from `total`; an owned job is present
- [x] Get: assert `GET /v1/jobs/{id}` returns 404 with `error: job_not_found` when
  `job.owner_id != "self"`; owner gets 200
- [x] Status counts: assert counts reflect only jobs owned by the resolved principal;
  cross-owner jobs are not counted
- [x] Retry: assert `POST /v1/jobs/{id}/retry` returns 404 with `error: job_not_found`
  on cross-owner job id
- [x] Cancel: assert `POST /v1/jobs/{id}/cancel` returns 404 with `error: job_not_found`
  on cross-owner job id
- [x] Bulk actions: assert a request mixing one owned and one cross-owner job id produces
  `status: "success"` for the owned job and `status: "error", error: "job_not_found"` for
  the cross-owner job; summary `errors` count includes the cross-owner job
