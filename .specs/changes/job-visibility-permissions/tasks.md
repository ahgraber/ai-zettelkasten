# Tasks: Job Visibility Permissions

## Wire principal dependency to read routes

- [ ] Add `principal: Annotated[Principal, Depends(get_principal)]` parameter to
  `list_jobs` in `src/aizk/conversion/api/routes/jobs.py`
- [ ] Add `principal: Annotated[Principal, Depends(get_principal)]` parameter to
  `get_job` in `src/aizk/conversion/api/routes/jobs.py`
- [ ] Add `principal: Annotated[Principal, Depends(get_principal)]` parameter to
  `get_job_status_counts` in `src/aizk/conversion/api/routes/jobs.py`

## Owner-scope list and count queries

- [ ] Add `.where(ConversionJob.owner_id == principal.subject)` to the `query` and
  `count_query` statements in `list_jobs` (alongside existing filters)
- [ ] Add `.where(ConversionJob.owner_id == principal.subject)` to the count query
  in `get_job_status_counts`

## Owner check on get and mutation routes

- [ ] In `get_job`: after the 404-on-missing check, add
  `if job.owner_id != principal.subject: raise HTTPException(404, {"error": "job_not_found", ...})`
- [ ] In `retry_job`: after the 404-on-missing check, add the same owner check;
  remove the `# noqa: ARG001` comment
- [ ] In `cancel_job`: after the 404-on-missing check, add the same owner check;
  remove the `# noqa: ARG001` comment
- [ ] In `bulk_job_actions`: inside the per-job loop, after the not-found check,
  add an owner check that appends `BulkActionResult(job_id=job_id, status="error", error="job_not_found")`
  and increments `errors` on mismatch; remove the `# noqa: ARG001` comment

## Tests

- [ ] `tests/conversion/unit/api/routes/test_jobs.py` — `list_jobs`: assert that a job
  with a different `owner_id` is absent from results and excluded from `total`
- [ ] `tests/conversion/unit/api/routes/test_jobs.py` — `get_job`: assert HTTP 404
  with `error: job_not_found` when `job.owner_id != principal.subject`
- [ ] `tests/conversion/unit/api/routes/test_jobs.py` — `get_job_status_counts`: assert
  counts reflect only jobs owned by `principal.subject`
- [ ] `tests/conversion/unit/api/routes/test_jobs.py` — `retry_job`: assert HTTP 404
  with `error: job_not_found` on cross-owner job id
- [ ] `tests/conversion/unit/api/routes/test_jobs.py` — `cancel_job`: assert HTTP 404
  with `error: job_not_found` on cross-owner job id
- [ ] `tests/conversion/unit/api/routes/test_jobs.py` — `bulk_job_actions`: assert
  that a cross-owner job id produces `status: "error", error: "job_not_found"` in the
  per-job result while an owned job in the same request is actioned successfully
