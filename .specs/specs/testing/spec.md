# Testing Specification

## Purpose

This capability defines the project-wide contract for how automated tests interact with workstation configuration state.
It exists so that test outcomes depend only on inputs the test harness controls — not on developer-machine artifacts that vary across workstations or between local and CI environments.

## Requirements

### Requirement: Test outcomes are hermetic with respect to workstation configuration state

The result of any test in this project SHALL be determined solely by the test's own inputs and fixtures.
Test outcomes SHALL NOT vary based on the contents of any configuration source that pydantic-settings (or equivalent settings loader) would read from the developer's machine outside the test tree — in particular, `.env` files on disk and environment variables already present in the shell when the test process starts.

#### Scenario: Identical test run on workstations with differing `.env` contents

- **GIVEN** two workstations running the same test suite against the same commit
- **AND** one workstation has a populated `.env` that would, if read, override configuration fields
- **AND** the other workstation has no `.env` file
- **WHEN** the test suite executes on each
- **THEN** every test produces the same pass/fail outcome on both workstations

#### Scenario: Identical test run on workstations with differing shell environment

- **GIVEN** two workstations running the same test suite against the same commit
- **AND** one workstation has shell-exported variables that correspond to `ConversionConfig` fields (for example `DOCLING_PICTURE_DESCRIPTION_MODEL` set in a shell profile)
- **AND** the other workstation has none of those variables set
- **WHEN** the test suite executes on each
- **THEN** every test produces the same pass/fail outcome on both workstations

#### Scenario: CI run vs. local run

- **GIVEN** a developer running the suite locally with a populated `.env` and arbitrary shell env vars
- **AND** CI running the suite on a container with a clean environment
- **WHEN** both runs execute the same commit
- **THEN** the set of passing and failing tests is identical between the two runs

### Requirement: Configuration instances are populated only from sources the test controls

Any `ConversionConfig` (or equivalent `BaseSettings` subclass) instance that exists during a test run — whether constructed directly by a test, produced by a fixture, or attached to an application under test — SHALL derive its field values only from sources the test harness controls: explicit constructor arguments, monkeypatched environment variables set within the test tree, or library defaults.
It SHALL NOT read an environment file from disk, and it SHALL NOT inherit values from shell-exported environment variables present when the test process started.

#### Scenario: Direct construction in a test body

- **GIVEN** a test that needs a `ConversionConfig` instance
- **WHEN** the test constructs one
- **THEN** the construction does not read `.env` from the repository or working directory
- **AND** field values come from explicit arguments, monkeypatched environment variables, or library defaults — nothing else

#### Scenario: Shell-exported variable is not inherited by a config

- **GIVEN** a developer has exported a variable (e.g., `DOCLING_PICTURE_DESCRIPTION_MODEL=custom-model`) in the shell that launches pytest
- **AND** no fixture or test explicitly sets that variable
- **WHEN** a test constructs a `ConversionConfig` or uses the FastAPI app
- **THEN** the resulting config has the library default for that field, not `custom-model`

#### Scenario: Config attached to a FastAPI app under test

- **GIVEN** a test that uses `create_app()` or `TestClient` to exercise the conversion API
- **WHEN** the app's lifespan runs
- **THEN** the `ConversionConfig` accessible via `app.state.config` was constructed without reading `.env` from disk and without inheriting shell-exported variables the harness did not claim

#### Scenario: Test-side config agrees with app-side config

- **GIVEN** a test that constructs its own `ConversionConfig` for assertions
- **AND** the same test exercises a FastAPI app whose lifespan constructs its own `ConversionConfig`
- **WHEN** both configs are compared on any field the test depends on
- **THEN** the two configs agree, because neither has been influenced by `.env` or by uncontrolled shell state
