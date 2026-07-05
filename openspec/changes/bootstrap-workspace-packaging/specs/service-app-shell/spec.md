## ADDED Requirements

### Requirement: Service-app template exposes the reserved backend layout
The service-app template SHALL reserve the backend application, agent, configuration, eval, test, documentation, and environment file layout required by the Product Spec.

#### Scenario: Template directory structure exists
- **WHEN** a developer inspects `templates/service-app`
- **THEN** the template contains `app/`, `agents/`, `configs/profiles/`, `eval-cases/drafts/`, `eval-cases/approved/`, `tests/`, `docs/`, `.env.example`, `Makefile`, `README.md`, and `pyproject.toml`

### Requirement: App entry code is separated from agent logic
The template SHALL keep application entrypoints separate from future business agent implementation directories.

#### Scenario: App entry directories are reserved
- **WHEN** a developer inspects `templates/service-app/app`
- **THEN** it contains reserved `api/`, `cli/`, and `workers/` entrypoint areas

#### Scenario: Agent directories are reserved
- **WHEN** a developer inspects `templates/service-app/agents`
- **THEN** agent implementation is reserved under agent-specific directories instead of being placed inside `app/*`

### Requirement: Local profile shell runs without external provider credentials
The service-app shell SHALL provide a local development profile and smoke entrypoint that can run without real model keys or external observability providers.

#### Scenario: Local profile is present
- **WHEN** a developer inspects `templates/service-app/configs/profiles`
- **THEN** `local.yaml` exists and declares local defaults suitable for later fake provider and local-jsonl integration

#### Scenario: Local smoke entrypoint exists
- **WHEN** a developer runs the template local smoke command
- **THEN** the command completes using only workspace-local configuration and does not require real model keys or SaaS provider credentials

### Requirement: Template documentation identifies developer and maintainer entrypoints
The service-app template SHALL document how an agent application developer starts from the template and how a scaffold maintainer keeps the template aligned with the core package boundary.

#### Scenario: Template README describes both audiences
- **WHEN** a developer reads `templates/service-app/README.md`
- **THEN** the README identifies app developer setup steps and scaffold maintainer boundary responsibilities

