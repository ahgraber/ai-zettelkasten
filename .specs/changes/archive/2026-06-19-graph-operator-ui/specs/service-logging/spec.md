# Delta for service-logging

## ADDED Requirements

### Requirement: Process entrypoints configure logging through one shared mechanism

Every process entrypoint — each command of every stage's command-line interface (the conversion stage and the graph stage) — SHALL initialize logging through the single shared, centralized logging configuration before it performs any of the command's own work.
No entrypoint SHALL emit through the runtime's default last-resort logging handler.
Because all entrypoints share one configuration, every emitted record — including the forensic `extra` keys the egress-audit path attaches — is formatted uniformly regardless of which stage or command produced it.

#### Scenario: A long-running command configures logging before serving work

- **GIVEN** a long-running entrypoint (a stage's worker loop or its operator API server)
- **WHEN** the command starts
- **THEN** logging is configured through the shared mechanism before the loop or server begins handling work

#### Scenario: A one-shot command configures logging before its work

- **GIVEN** a one-shot entrypoint (such as schema initialization)
- **WHEN** the command runs
- **THEN** logging is configured through the shared mechanism before the command's work runs, rather than the work emitting through the default last-resort handler

#### Scenario: Entrypoints in different stages share one configuration

- **GIVEN** entrypoints belonging to more than one stage's CLI
- **WHEN** each configures logging at startup
- **THEN** they use the same centralized configuration, so their records carry the same structured format and pass arbitrary `extra` keys through identically
