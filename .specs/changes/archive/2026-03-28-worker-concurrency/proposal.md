# Proposal: Worker Concurrency

## Intent

The conversion-worker spec requires bounded concurrency (default: 4 parallel jobs), but the worker processes jobs sequentially.
A single 30-minute PDF conversion blocks the entire pipeline.
This change implements the existing spec requirement while accounting for the constraint that multiple concurrent Docling subprocesses sharing a single GPU will OOM.

## Scope

**In scope:**

- ThreadPoolExecutor-based concurrent job processing in the worker loop
- GPU-aware subprocess gating via a semaphore (`worker_gpu_concurrency` config)
- Shutdown drain of multiple concurrent in-flight jobs
- Extraction of `claim_next_job` from `poll_and_process_jobs` for poll/process separation

**Out of scope:**

- Multi-GPU device assignment (future work if hardware justifies it)
- Docling model sharing across processes (not supported by Docling)
- Async rewrite of the worker loop
- API-side changes (health endpoints, backpressure)
