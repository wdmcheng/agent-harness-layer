## Context

The repository currently has Product-Spec.md and DEV-PLAN.md as committed upstream truth, plus OpenSpec schema/templates but no active baseline specs. DEV-PLAN Phase 1 is the first implementation step: establish a uv workspace, a buildable core package, a service-app template shell, minimum quality commands, and compliance/documentation entrypoints.

This change intentionally creates scaffolding and public seams before runtime behavior. Later changes will add configuration, storage, runtime, policy, tools, retrieval, observability, eval, examples, CI, and release automation on top of these paths and commands.

Primary stakeholders:
- Agent application developers who need a copyable service-app shape.
- Scaffold maintainers who need a stable package boundary and quality command surface.
- Future OpenSpec changes that need durable paths for tasks and validation.

## Goals / Non-Goals

**Goals:**
- Establish the root uv workspace and lockfile workflow.
- Make `packages/agent-harness` buildable as wheel/sdist.
- Reserve the `templates/service-app` backend application layout without implementing business runtime.
- Provide root quality, test, smoke, build, and license-check commands with Phase 1 behavior.
- Add README, LICENSE, NOTICE, and pre-commit entrypoints that make boundaries explicit.

**Non-Goals:**
- Implement any durable runtime, DBOS adapter, storage schema, policy engine, HITL, tool execution, retrieval, observability provider, eval runner, API route behavior, or service-profile Docker orchestration.
- Implement the four P0 example agents beyond directories or empty package markers.
- Add a product UI or SaaS management surface.
- Vendor upstream SDK source code.

## Decisions

1. Use uv workspace as the only Python dependency and workspace manager.
   - Rationale: Product-Spec.md requires uv workspace and DEV-PLAN.md pins uv as the package manager. A single dependency manager prevents lockfile and script drift.
   - Alternative considered: Poetry or plain pip requirements. Rejected because they would contradict the upstream plan and add a second workflow.

2. Use `packages/agent-harness` as the only buildable core package in this change.
   - Rationale: The product needs a package boundary before feature modules. A minimal package with version export is enough for Phase 1 verification.
   - Alternative considered: Create all future module directories immediately. Rejected because empty deep directories invite fake completeness and make later changes harder to review.

3. Keep `templates/service-app` as a shell, not a runnable agent product.
   - Rationale: Phase 1 must reserve the app, CLI, worker, configs, eval, tests, and docs surfaces, but actual agent runtime belongs to later changes.
   - Alternative considered: Implement a full FastAPI route or fake agent run now. Rejected because it would pull Phase 5 and Phase 12 concerns into the workspace bootstrap.

4. Define quality commands early, even if some commands only validate Phase 1 seams.
   - Rationale: Later changes need stable command names: `make quality`, `make test`, `make smoke-local`, `make build`, and `make license-check`.
   - Alternative considered: Add commands only when each subsystem exists. Rejected because it delays the contract needed by dev-builder and CI.

5. Use import boundary checks as a Phase 1 contract.
   - Rationale: The Product Spec repeatedly depends on upstream isolation. A simple script can enforce that the core package does not depend on templates/examples and that early template code does not bypass the package boundary.
   - Alternative considered: Rely on code review only. Rejected because this boundary is mechanical and should be checked by commands.

6. Add Apache-2.0 license and NOTICE immediately.
   - Rationale: Compliance is a P0 product requirement and affects dependency and vendoring decisions from the first commit.
   - Alternative considered: Add license artifacts near release. Rejected because it would leave early copied snippets and dependency decisions unaudited.

## Affected Surfaces

- Root project files: `pyproject.toml`, `uv.lock`, `Makefile`, `.pre-commit-config.yaml`, `README.md`, `LICENSE`, `NOTICE`.
- Core package: `packages/agent-harness/pyproject.toml`, `packages/agent-harness/src/agent_harness/__init__.py`, package metadata and tests.
- Template shell: `templates/service-app/pyproject.toml`, `templates/service-app/Makefile`, `.env.example`, README, reserved `app/`, `agents/`, `configs/profiles/`, `eval-cases/`, `tests/`, and `docs/` paths.
- Scripts: import-boundary check, license check, and any minimal smoke helper needed by root commands.
- Tests: Phase 1 tests only cover package import/build metadata, workspace shape, command wiring, and boundary checks.
- APIs: No runtime HTTP API behavior.
- Data models and migrations: None.
- Release concerns: Buildable wheel/sdist is included; release automation is not.

## Testing Seams

- `uv sync` at repository root resolves the workspace.
- `uv build --package agent-harness` produces wheel/sdist artifacts.
- `python -c "import agent_harness; print(agent_harness.__version__)"` succeeds in the workspace or from an installed build artifact.
- `make quality` runs ruff, pyright, and import-boundary checks for Phase 1 files.
- `make test` runs Phase 1 unit/contract tests.
- `make smoke-local` verifies the workspace and service-app shell without external model keys or SaaS providers.
- `make license-check` verifies `LICENSE`, `NOTICE`, and no undeclared vendored source baseline.
- README inspection covers directory responsibilities and forbidden dependency directions.

## Risks / Trade-offs

- [Risk] Scaffolding can look complete while behavior is still absent. -> Mitigation: README, proposal, specs, and tasks must state that runtime/storage/policy/eval are non-goals for this change.
- [Risk] Quality commands may become too weak if they only check empty files. -> Mitigation: include boundary, import, package build, and smoke checks that validate public seams rather than private implementation.
- [Risk] Adding all future dependencies now can increase lockfile churn and mask Phase 1 scope. -> Mitigation: keep runtime dependencies out unless needed for package metadata groups; add subsystem dependencies in their own changes.
- [Risk] Reserved directories can become dead structure. -> Mitigation: create only directories required by Product-Spec.md and include README notes that future changes own behavior.
- [Risk] License check can overreach before real dependencies exist. -> Mitigation: start with Apache-2.0 and NOTICE presence checks plus vendoring detection; expand dependency license auditing in release/compliance changes.

## Migration Plan

This is the first baseline change and has no data migration.

Implementation path:
- Create workspace and package/template shell files.
- Add root command wrappers and Phase 1 validation scripts.
- Run `uv sync`, `make quality`, `make test`, `make smoke-local`, `make build`, and `make license-check`.
- Keep Product-Spec.md and DEV-PLAN.md unchanged unless implementation reveals a real upstream requirement conflict.

Rollback strategy:
- Revert the change files and remove generated lock/build artifacts if the workspace bootstrap blocks local development.
- Because no data schema or runtime state is introduced, rollback is file-level only.

## Open Questions

- Should the root package version start at `0.1.0` or `0.0.0` before release automation exists?
- Should pyright run through the Python `pyright` package, an npm-managed binary, or both in CI later?
- Should Phase 1 include dependency license scanning for all locked packages, or reserve full dependency license audit for the release/compliance change?
