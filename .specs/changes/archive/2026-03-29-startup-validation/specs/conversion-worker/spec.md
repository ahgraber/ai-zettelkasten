# Delta for Conversion Worker

## ADDED Requirements

### Requirement: Validate required external services on startup

The system SHALL probe required external services (S3 storage and KaraKeep API) at process startup and SHALL refuse to start if any required service is unreachable.
Probes SHALL use bounded timeouts to avoid hanging on unresponsive services.

#### Scenario: S3 reachable at startup

- **GIVEN** valid S3 credentials and endpoint are configured
- **WHEN** the worker or API process starts
- **THEN** a HEAD bucket probe succeeds within the timeout and the process continues startup

#### Scenario: S3 unreachable at startup

- **GIVEN** S3 credentials are invalid or the endpoint is unreachable
- **WHEN** the worker or API process starts
- **THEN** the process logs a structured error identifying the S3 failure and exits with a non-zero exit code

#### Scenario: KaraKeep API reachable at startup

- **GIVEN** a valid KaraKeep base URL and API key are configured
- **WHEN** the worker or API process starts
- **THEN** a health probe to the KaraKeep API succeeds within the timeout and the process continues startup

#### Scenario: KaraKeep API unreachable at startup

- **GIVEN** the KaraKeep API is unreachable or returns an error
- **WHEN** the worker or API process starts
- **THEN** the process logs a structured error identifying the KaraKeep failure and exits with a non-zero exit code

### Requirement: Log optional feature status summary on startup

The system SHALL log a structured summary of all optional feature states on startup, indicating which features are enabled and which are disabled with the reason (missing configuration).

#### Scenario: All optional features enabled

- **GIVEN** chat completions, MLflow tracing, and Litestream replication are all configured
- **WHEN** the process starts
- **THEN** the startup summary log entry lists all three features as enabled

#### Scenario: Optional feature disabled due to missing config

- **GIVEN** the chat completions base URL or API key is not configured
- **WHEN** the process starts
- **THEN** the startup summary log entry lists picture descriptions as disabled with the reason "chat completions endpoint not configured"

#### Scenario: Multiple features disabled

- **GIVEN** MLflow tracing and Litestream replication are both unconfigured
- **WHEN** the process starts
- **THEN** the startup summary log entry lists both features as disabled with their respective reasons

## MODIFIED Requirements

### Requirement: Load configuration from environment variables

The system SHALL load all configuration from environment variables with sensible defaults for local development, and SHALL validate required service reachability before entering the main processing loop. (Previously: configuration was loaded without any reachability validation.)
