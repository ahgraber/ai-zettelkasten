# Proposal: Worker Graceful Shutdown

## Intent

The worker-process-management spec defines subprocess lifecycle (termination, cleanup, cancellation) but is silent on what happens when the worker process itself receives SIGTERM or SIGINT.
During routine deployments, container restarts, or systemd stops, the worker loop is interrupted mid-flight — leaving jobs stranded in RUNNING state for up to 30 minutes (until stale recovery), potentially orphaning subprocesses, and risking partial S3 uploads with no cleanup.

## Scope

**In scope:**

- Worker process signal handling (SIGTERM, SIGINT)
- Draining in-flight jobs before exit
- Bounded drain timeout to prevent infinite hangs
- Clean state on exit (no jobs left in RUNNING, no orphan processes)

**Out of scope:**

- API process shutdown (uvicorn handles SIGTERM with its own drain)
- Worker concurrency model (separate change)
- Health check endpoints (separate change)
- Changes to job-level subprocess termination (already spec'd)

## Approach

Add a new requirement to the existing worker-process-management spec covering the worker loop's own signal handling.
The spec requires that SIGTERM/SIGINT trigger a drain mode: stop accepting new jobs, wait for in-flight work to complete (with a bounded timeout), then exit cleanly.
This extends the existing process group termination and workspace cleanup guarantees to the worker process itself.
