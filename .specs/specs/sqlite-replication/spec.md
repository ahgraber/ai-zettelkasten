# SQLite Replication Specification

> Generated from code analysis on 2026-04-15
> Source files: `src/aizk/conversion/utilities/litestream.py`, `tests/conversion/unit/test_litestream.py`

## Purpose

When enabled, the conversion service replicates its SQLite database to S3-compatible object storage via Litestream, providing durability and cross-instance restore without requiring a centralized database.
Replication is an optional, opt-in feature; when disabled, the service SHALL remain fully functional using only the local SQLite file.

This specification describes the contracts that hold **when replication is configured**.
Behavior when replication is disabled is limited to inertness: no subprocess spawned, no config file emitted, no external calls made.

## Requirements

### Requirement: Emitted replica configuration conforms to the Litestream S3 schema

The system SHALL emit a Litestream configuration file that describes exactly one database entry keyed by the absolute local database path, targeting a named S3 bucket at a region-qualified relative object key.
Optional transport fields (custom endpoint, path-style addressing, payload signing) SHALL appear only when the caller explicitly configures them, so the resulting config reflects only intentional overrides.

#### Scenario: Required replica fields are populated

- **GIVEN** a configured database path, bucket name, S3 prefix, and region
- **WHEN** the replication configuration file is generated
- **THEN** the file declares the database path and a single S3 replica whose `bucket`, `path` (prefix + database filename), and `region` match the supplied values

#### Scenario: Optional transport fields are elided when unset

- **GIVEN** no custom endpoint is configured and path-style/payload-signing overrides are left at their defaults
- **WHEN** the configuration file is generated
- **THEN** the emitted replica omits `endpoint`, `force-path-style`, and `sign-payload` entirely rather than serializing null or false values

---

### Requirement: Replica path safety invariants are enforced at emission time

The system SHALL reject configurations whose paths would cause Litestream to operate outside its expected filesystem or S3-key conventions.
Validation SHALL occur when the config is constructed, so misconfiguration surfaces before any subprocess is launched.

#### Scenario: Relative database paths are rejected

- **GIVEN** a database path that is not absolute
- **WHEN** the configuration is constructed
- **THEN** a `ValueError` whose message identifies the absolute-path requirement is raised

#### Scenario: S3 replica paths must be relative object keys

- **GIVEN** an S3 replica path beginning with `/`
- **WHEN** the replica configuration is constructed
- **THEN** a validation error is raised rejecting the leading slash

---

### Requirement: Replication processes run as isolated process groups with reliable teardown

The system SHALL launch the replication process in its own process group and SHALL, on stop, terminate the whole group so that no Litestream descendant outlives the manager.
Termination SHALL escalate from graceful to forceful on timeout, and teardown without a corresponding start SHALL be a no-op.

#### Scenario: Start followed by stop leaves no surviving threads or process handles

- **GIVEN** a started replication manager with a running subprocess
- **WHEN** `stop()` is invoked
- **THEN** the process group is signalled for termination, the manager waits for the process to exit, and no thread outlives the manager

#### Scenario: Stop without start is a no-op

- **GIVEN** a replication manager that has never been started
- **WHEN** `stop()` is invoked
- **THEN** the call returns without raising and without signalling any process

#### Scenario: Graceful termination escalates on timeout

- **GIVEN** a replication process that does not exit within the graceful shutdown window after SIGTERM
- **WHEN** the termination timeout elapses
- **THEN** the system sends SIGKILL to the process group and tolerates the race where the process has already exited

---

### Requirement: Replication participates only when fully eligible

The system SHALL start replication only when all of the following hold: replication is enabled by configuration, the manager's role is included in the configured role set (`both` or explicit), and the database URL resolves to a file-based SQLite path.
Any disqualifying condition SHALL cause the manager to remain inert without raising, so ineligible deployments degrade to no-replication cleanly.

#### Scenario: Replication disabled by configuration

- **GIVEN** replication is configured as disabled
- **WHEN** the manager is started
- **THEN** no subprocess is spawned and start returns normally

#### Scenario: Role not included in configured role set

- **GIVEN** replication is enabled but the configured role set excludes the manager's role and does not contain `both`
- **WHEN** the manager is started
- **THEN** no subprocess is spawned and start returns normally

#### Scenario: Non-file SQLite URLs are skipped

- **GIVEN** a database URL that is in-memory, non-SQLite, or otherwise lacks a resolvable file path
- **WHEN** the manager is started
- **THEN** no subprocess is spawned and start returns normally

---

### Requirement: Restore-on-startup failure handling is opt-in

The system SHALL, by default, treat a failed restore-on-startup as a hard startup failure so missing or corrupt replicas do not silently produce an empty database.
Operators MAY opt into a permissive mode that converts restore failures into warnings and proceeds with replication, enabling first-boot scenarios where no prior replica exists.

#### Scenario: Default restore failure aborts startup

- **GIVEN** restore-on-startup is enabled and the restore command exits non-zero
- **WHEN** the manager is started against a missing local database
- **THEN** startup raises a `RuntimeError` carrying the restore command's output

#### Scenario: Permissive mode logs and continues

- **GIVEN** restore-on-startup is enabled, the restore command exits non-zero, and `allow_empty_restore` is enabled
- **WHEN** the manager is started
- **THEN** a warning is logged and replication proceeds without raising

---

## Technical Notes

- **Implementation:** `src/aizk/conversion/utilities/litestream.py`
- **Tests:** `tests/conversion/unit/test_litestream.py`
- **Dependencies:** `litestream` binary (must be on `PATH` or at configured absolute path), `yaml`, `pydantic`, `sqlalchemy.engine.make_url`, S3-compatible object store
- **Process Model:** Replication runs as a long-lived subprocess in its own process group (`os.setpgrp`) so the manager can signal the whole group atomically. `atexit` registration ensures cleanup on interpreter shutdown.
- **Scope Boundary:** This spec covers the manager lifecycle and configuration-file emission.
  It does NOT cover Litestream's internal WAL handling, checkpoint cadence, or S3 upload semantics, which are delegated to the Litestream binary.
