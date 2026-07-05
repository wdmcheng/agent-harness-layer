## ADDED Requirements

### Requirement: Root quality command surface exists
The repository SHALL provide root commands for dependency setup, quality checks, tests, local smoke verification, package build, and license checking.

#### Scenario: Quality command executes
- **WHEN** a developer runs `make quality`
- **THEN** the command runs linting, formatting checks, type checking, and import boundary checks for the current Phase 1 code surface

#### Scenario: Test command executes
- **WHEN** a developer runs `make test`
- **THEN** the command runs the Phase 1 unit and contract test suite

#### Scenario: Local smoke command executes
- **WHEN** a developer runs `make smoke-local`
- **THEN** the command verifies the local workspace and template shell without external service dependencies

#### Scenario: Build command executes
- **WHEN** a developer runs `make build`
- **THEN** the command builds the core package artifacts through uv

#### Scenario: License check command executes
- **WHEN** a developer runs `make license-check`
- **THEN** the command verifies the expected license and NOTICE baseline

### Requirement: Documentation states repository purpose and boundaries
The root README SHALL explain what the scaffold is, how to start locally, the project structure, and the forbidden cross-boundary dependencies.

#### Scenario: README explains project structure
- **WHEN** a new developer reads the root README
- **THEN** they can identify the purpose of `packages/agent-harness`, `templates/service-app`, `examples`, `docs`, and `scripts`

#### Scenario: README explains dependency boundaries
- **WHEN** a scaffold maintainer reads the root README
- **THEN** they can identify that the core package does not depend on templates or examples and that vendor SDKs belong behind adapters or future controlled integration modules

### Requirement: License and NOTICE baseline exists
The repository MUST include an Apache-2.0 license file and a NOTICE file before feature development starts.

#### Scenario: License file is present
- **WHEN** a developer checks the repository root
- **THEN** `LICENSE` exists and declares Apache-2.0

#### Scenario: NOTICE file is present
- **WHEN** a developer checks the repository root
- **THEN** `NOTICE` exists as the place for required third-party notices and source attributions

### Requirement: Pre-commit entrypoint exists
The repository SHALL provide a pre-commit configuration that points at the Phase 1 quality checks.

#### Scenario: Pre-commit configuration is present
- **WHEN** a developer inspects the repository root
- **THEN** `.pre-commit-config.yaml` exists and can be installed for local quality checks

