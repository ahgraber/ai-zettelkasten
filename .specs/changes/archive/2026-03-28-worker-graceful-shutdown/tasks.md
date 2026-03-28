# Tasks: Worker Graceful Shutdown

## Spec

- [x] Sync delta spec into worker-process-management baseline

## Config

- [x] Add `worker_drain_timeout_seconds: int = 300` to `ConversionConfig`

## Signal Handling

- [x] Add shutdown event and signal registration function that handles SIGTERM and SIGINT
- [x] Wire signal registration into `run_worker()` startup
- [x] Handle second signal during drain as immediate forced termination

## Worker Loop

- [x] Modify `run_worker()` to check shutdown event before each poll cycle
- [x] Implement drain logic: after signal, wait for in-flight job up to drain timeout
- [x] On drain timeout, terminate in-flight subprocess via existing termination sequence
- [x] Transition force-terminated jobs to FAILED_RETRYABLE and clean up workspaces
- [x] Exit with code 0 on clean drain, code 1 on forced termination

## Logging

- [x] Log signal received (signal name) at INFO level
- [x] Log drain started with count of in-flight jobs
- [x] Log each job completion or forced termination during drain
- [x] Log final exit with exit code

## Tests

- [x] Test: signal when idle exits immediately
- [x] Test: signal with in-flight job waits for completion
- [x] Test: drain timeout triggers forced termination and FAILED_RETRYABLE transition
- [x] Test: second signal during drain triggers immediate termination
- [x] Test: no jobs left in RUNNING state after exit
