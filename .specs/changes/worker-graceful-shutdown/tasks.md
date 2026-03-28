# Tasks: Worker Graceful Shutdown

## Spec

- [ ] Sync delta spec into worker-process-management baseline

## Config

- [ ] Add `worker_drain_timeout_seconds: int = 300` to `ConversionConfig`

## Signal Handling

- [ ] Add shutdown event and signal registration function that handles SIGTERM and SIGINT
- [ ] Wire signal registration into `run_worker()` startup
- [ ] Handle second signal during drain as immediate forced termination

## Worker Loop

- [ ] Modify `run_worker()` to check shutdown event before each poll cycle
- [ ] Implement drain logic: after signal, wait for in-flight job up to drain timeout
- [ ] On drain timeout, terminate in-flight subprocess via existing termination sequence
- [ ] Transition force-terminated jobs to FAILED_RETRYABLE and clean up workspaces
- [ ] Exit with code 0 on clean drain, code 1 on forced termination

## Logging

- [ ] Log signal received (signal name) at INFO level
- [ ] Log drain started with count of in-flight jobs
- [ ] Log each job completion or forced termination during drain
- [ ] Log final exit with exit code

## Tests

- [ ] Test: signal when idle exits immediately
- [ ] Test: signal with in-flight job waits for completion
- [ ] Test: drain timeout triggers forced termination and FAILED_RETRYABLE transition
- [ ] Test: second signal during drain triggers immediate termination
- [ ] Test: no jobs left in RUNNING state after exit
