# Tasks: Worker Concurrency

## Config

- [x] Add `worker_gpu_concurrency: int = 1` to `ConversionConfig`

## Orchestrator

- [x] Add module-level GPU semaphore and `configure_gpu_semaphore()` function
- [x] Wrap subprocess spawn + supervise in semaphore acquire/release in `process_job_supervised`

## Worker Loop

- [x] Extract `claim_next_job()` from `poll_and_process_jobs()`
- [x] Rewrite `run_worker()` to use `ThreadPoolExecutor` with greedy slot filling
- [x] Add `_reap_completed()` helper for done future cleanup
- [x] Add `_drain_in_flight()` helper for shutdown drain of multiple futures

## Tests

- [x] Test `claim_next_job` returns job_id or None
- [x] Test `run_worker` respects `worker_concurrency` limit
- [x] Test GPU semaphore limits concurrent subprocess spawning
- [x] Test shutdown drains all in-flight jobs
- [x] Test exit code 0 on clean drain, 1 on timeout or double-signal
- [x] Update existing `test_worker_shutdown.py` for new loop structure

## Spec Sync

- [x] Sync delta specs into baseline specs
