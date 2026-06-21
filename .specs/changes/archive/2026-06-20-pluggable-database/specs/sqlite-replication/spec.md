# Delta for sqlite-replication

## REMOVED Requirements

### Requirement: Replication participates only when fully eligible

> Removed: rescoped to the SQLite backend and renamed — replaced by the ADDED
> "Replication participates only when the SQLite backend is eligible" below.
> Eligibility was previously stated only over the database URL ("resolves to a
> file-based SQLite path"), and a non-SQLite URL was silently inert in this
> manager. Backend selection now rejects unsupported backends up front, so this
> manager's eligibility narrows to the SQLite backend plus a resolvable file path.

## ADDED Requirements

### Requirement: Replication participates only when the SQLite backend is eligible

The system SHALL start replication only when all of the following hold: the active database backend is SQLite, replication is enabled by configuration, the manager's role is included in the configured role set (`both` or explicit), and the database resolves to a file-based path.
Any disqualifying condition SHALL cause the manager to remain inert without raising, so an eligible-but-unconfigured SQLite deployment degrades to no-replication cleanly.
Litestream replication is the SQLite backend's durability mechanism; a non-SQLite backend is rejected at backend selection and never reaches this manager, and the durability of any such backend is defined by that backend's own capability.

Serves: durability-matched-to-backend

#### Scenario: Replication disabled by configuration

- **GIVEN** the SQLite backend is active and replication is configured as disabled
- **WHEN** the manager is started
- **THEN** no subprocess is spawned and start returns normally

#### Scenario: Role not included in configured role set

- **GIVEN** the SQLite backend is active, replication is enabled, but the configured role set excludes the manager's role and does not contain `both`
- **WHEN** the manager is started
- **THEN** no subprocess is spawned and start returns normally

#### Scenario: A file-less SQLite database is skipped

- **GIVEN** the SQLite backend is active but the database is in-memory or otherwise lacks a resolvable file path
- **WHEN** the manager is started
- **THEN** no subprocess is spawned and start returns normally
