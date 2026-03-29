# Delta for Conversion Worker

## ADDED Requirements

### Requirement: Limit concurrent GPU subprocess spawning

The system SHALL limit the number of concurrently running conversion subprocesses to a configurable maximum (default: 1) to prevent GPU memory exhaustion.
Job phases that do not use the GPU (preflight, upload) SHALL run without this limit, enabling pipeline parallelism.

#### Scenario: GPU concurrency limit enforced

- **GIVEN** the GPU concurrency limit is 1 and one conversion subprocess is already running
- **WHEN** a second job reaches the conversion phase
- **THEN** the second job blocks until the first subprocess completes before spawning its own subprocess

#### Scenario: Non-GPU phases run concurrently

- **GIVEN** the GPU concurrency limit is 1 and one job is converting on GPU
- **WHEN** other jobs are in preflight or upload phases
- **THEN** those phases proceed without waiting for the GPU slot

## MODIFIED Requirements

### Requirement: Process jobs with bounded concurrency in FIFO order

The system SHALL process conversion jobs with a configurable number of parallel worker threads (default: 4) in first-in-first-out order by queue time.
The main thread claims jobs atomically and dispatches them to worker threads. (Previously: the system SHALL process conversion jobs with a configurable number of parallel workers (default: 4) in first-in-first-out order by queue time.)

#### Scenario: Concurrent jobs processed up to limit

- **GIVEN** more jobs are queued than the concurrency limit
- **WHEN** the main thread polls for work
- **THEN** at most the configured number of jobs run simultaneously in worker threads and jobs are selected in queue-time order

#### Scenario: Main thread fills worker slots greedily

- **GIVEN** multiple jobs are queued and worker slots are available
- **WHEN** the main thread polls for work
- **THEN** it claims and dispatches jobs until all worker slots are filled or no more jobs are eligible
