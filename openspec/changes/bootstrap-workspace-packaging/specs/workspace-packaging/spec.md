## ADDED Requirements

### Requirement: Workspace resolves core and template packages
The repository SHALL define a `uv workspace` that includes the core package and service-app template as workspace members.

#### Scenario: Workspace sync resolves members
- **WHEN** a developer runs `uv sync` at the repository root
- **THEN** uv resolves the workspace without dependency errors for `packages/agent-harness` and `templates/service-app`

#### Scenario: Workspace structure exposes expected top-level boundaries
- **WHEN** a developer inspects the repository root
- **THEN** the repository exposes `packages/`, `templates/`, `examples/`, `docs/`, and `scripts/` as separate top-level areas

### Requirement: Core package builds independently
The `agent-harness` core package SHALL be buildable as wheel and sdist without importing template or example code.

#### Scenario: Core package build succeeds
- **WHEN** a developer runs `uv build --package agent-harness`
- **THEN** the build produces wheel and sdist artifacts for the core package

#### Scenario: Core package import succeeds
- **WHEN** a developer imports `agent_harness` from an installed build artifact or workspace environment
- **THEN** the import succeeds and exposes a package version value

### Requirement: Package dependency direction is enforced
The core package MUST NOT depend on `templates/*` or `examples/*`, and the service-app template SHALL depend on `agent-harness` through a workspace/path dependency or a built wheel.

#### Scenario: Core package has no reverse dependency
- **WHEN** dependency metadata and import boundary checks are run
- **THEN** `packages/agent-harness` does not reference `templates/*` or `examples/*`

#### Scenario: Template depends on the core package through the package boundary
- **WHEN** the service-app template is installed in the workspace
- **THEN** it resolves `agent-harness` through the declared package dependency instead of importing source by relative path

