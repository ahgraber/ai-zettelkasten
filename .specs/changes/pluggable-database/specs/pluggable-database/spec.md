# Delta for pluggable-database

## ADDED Requirements

### Requirement: Database access is a shared, stage-independent foundation

The database engine, the schema-migration runner, and the database configuration SHALL be provided by a shared database foundation that no application stage owns.
Any stage that reads or writes the database SHALL obtain the engine, run migrations, and resolve the database URL through that shared foundation rather than through another stage.

Serves: database-is-shared-foundation

#### Scenario: A stage obtains the database without depending on another stage

- **GIVEN** a stage other than the one that historically hosted the database layer needs the engine and migrations
- **WHEN** it resolves them
- **THEN** it resolves them from the shared database foundation, not by importing another stage's module

#### Scenario: One migration tree governs every stage's tables

- **GIVEN** tables owned by more than one stage
- **WHEN** migrations are run to head through the shared foundation
- **THEN** a single migration tree produces the full schema covering every stage's tables

### Requirement: The active database backend is selected deterministically and fails closed

The active database backend SHALL be determined deterministically from configuration (the configured database URL), exposed as an explicit backend identity.
A configured backend that is not supported SHALL cause startup to fail with a clear error, rather than silently defaulting to another backend or proceeding in a half-configured state.

Serves: choose-database-backend

#### Scenario: A supported backend URL selects that backend

- **GIVEN** a database URL for a supported backend
- **WHEN** configuration is resolved
- **THEN** the backend identity reflects that backend and the application uses it

#### Scenario: An unsupported backend fails closed at startup

- **GIVEN** a database URL whose backend is not supported (an unrecognized scheme, or one whose backend arm is not yet implemented)
- **WHEN** configuration is resolved at startup
- **THEN** startup fails with an error naming the unsupported backend, rather than proceeding
