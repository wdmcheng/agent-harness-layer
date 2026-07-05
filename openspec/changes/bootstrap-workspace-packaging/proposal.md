## Source Links

- Product-Spec.md: `SCOPE-001` through `SCOPE-003` for uv workspace, package boundary, and service-app template; `SCOPE-022`, `SCOPE-023`, and `SCOPE-026` for README, quality gate, and Apache-2.0 compliance.
- Product-Spec.md: `REQ-001` Monorepo / uv workspace structure; `REQ-002` core package and upstream isolation; `REQ-003` backend service-app template; `REQ-018` README entrypoint; `REQ-019` TDD and quality gate; `REQ-021` license and compliance.
- DEV-PLAN.md: `Phase 1: Monorepo 骨架与质量门禁地基`.
- Design-Brief.md or design artifact: No Design-Brief exists. This change has no product UI surface.
- CONTEXT.md / ADR: None.

## Why

The product cannot safely start runtime, storage, policy, or eval work until the repository has a durable workspace shape, package boundary, and minimum verification spine. This change creates the Phase 1 baseline so later OpenSpec changes can target stable paths and commands instead of negotiating project structure again.

## What Changes

- Add a `uv workspace` root that separates `packages/agent-harness`, `templates/service-app`, `examples`, `docs`, and `scripts`.
- Add an installable `agent-harness` package skeleton that can build wheel/sdist and expose a versioned public package entrypoint.
- Add a `templates/service-app` shell that depends on `agent-harness` through the workspace/path dependency and reserves the app, agents, config, eval, tests, docs, and environment layout from the Product Spec.
- Add minimum developer commands for dependency sync, quality checks, tests, local smoke, package build, and license checking.
- Add README, LICENSE, NOTICE, and pre-commit entrypoints that document the scaffold purpose, directory boundaries, and compliance baseline.

## Non-Goals

- Do not implement runtime orchestration, DBOS integration, storage repositories, migrations, event streaming, policy, HITL, tools, retrieval, observability adapters, eval runner, or CI release automation.
- Do not implement the four example agents beyond empty package markers and directory structure required for the service-app shell.
- Do not add product UI, SaaS management screens, user registration, OAuth/OIDC, or service-profile Docker orchestration.
- Do not vendor Pydantic AI, DBOS, Logfire, Phoenix, Langfuse, or other upstream SDK source code.

## Capabilities

### New Capabilities

- `workspace-packaging`: Defines the uv workspace, installable core package, build boundary, and package dependency relationship.
- `service-app-shell`: Defines the backend service-app template shell, reserved directory layout, local profile entrypoints, and boundary between app entry code and future agent logic.
- `quality-compliance-entrypoints`: Defines the minimum quality, smoke, documentation, and license/compliance entrypoints needed before feature development starts.

### Modified Capabilities

- None. No existing OpenSpec baseline specs exist yet.

## Impact

- Affected code and files: root `pyproject.toml`, `uv.lock`, `Makefile`, `.pre-commit-config.yaml`, `README.md`, `LICENSE`, `NOTICE`, `packages/agent-harness/**`, `templates/service-app/**`, `examples/**`, `docs/**`, and `scripts/**`.
- Affected APIs: no runtime HTTP API is introduced; only package entrypoints and developer commands are established.
- Affected dependencies: uv, hatchling, ruff, pyright, pytest, pytest-asyncio, coverage.py, pre-commit, and license-check tooling. Runtime dependencies such as FastAPI, Pydantic AI, DBOS, SQLAlchemy, and provider SDKs remain out of this change unless needed only as declared optional future dependency groups without implementation.
- Affected data: no database schema or migrations.
- Affected UI surfaces: none.
- Affected systems: local developer workflow and future CI command contracts.
