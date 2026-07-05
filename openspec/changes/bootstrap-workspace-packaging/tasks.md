## 1. Workspace and Core Package

- [x] 1.1 Create the root `pyproject.toml` uv workspace with `packages/agent-harness` and `templates/service-app` as members; verify with `uv sync`.
- [x] 1.2 Create `packages/agent-harness/pyproject.toml` with hatchling build metadata and the `agent-harness` package name; verify metadata is visible through uv.
- [x] 1.3 Create `packages/agent-harness/src/agent_harness/__init__.py` with a package version export; verify `python -c "import agent_harness; print(agent_harness.__version__)"` works inside the workspace.
- [x] 1.4 Configure `uv build --package agent-harness` to produce wheel and sdist artifacts; verify the build command succeeds.
- [x] 1.5 Create the top-level `packages/`, `templates/`, `examples/`, `docs/`, and `scripts/` boundaries required by the workspace spec; verify the root layout exists.

## 2. Service-App Template Shell

- [x] 2.1 Create `templates/service-app/pyproject.toml` with a workspace/path dependency on `agent-harness`; verify `uv sync` resolves it without relative source imports.
- [x] 2.2 Create the reserved template layout under `templates/service-app/app/{api,cli,workers}`, `agents/`, `configs/profiles/`, `eval-cases/{drafts,approved}`, `tests/`, and `docs/`; verify the paths exist.
- [x] 2.3 Add `templates/service-app/configs/profiles/local.yaml` with minimal local-profile defaults for later fake provider and local-jsonl integration; verify the file is present and parseable.
- [x] 2.4 Add `templates/service-app/.env.example` documenting the local profile switch and absence of required real provider keys for Phase 1; verify template smoke does not require secrets.
- [x] 2.5 Add `templates/service-app/README.md` with app developer startup notes and scaffold maintainer boundary notes; verify both audiences are explicitly named.
- [x] 2.6 Add a Phase 1 `templates/service-app/Makefile` or root-dispatched template smoke command; verify the local smoke command completes without external services.

## 3. Quality and Compliance Entrypoints

- [x] 3.1 Add root `Makefile` commands for `quality`, `test`, `smoke-local`, `build`, and `license-check`; verify each command is callable from the repository root.
- [x] 3.2 Add ruff and pyright configuration for the Phase 1 code surface; verify `make quality` runs both tools.
- [x] 3.3 Add `scripts/import_boundary_check.py` to reject core package dependencies on templates/examples and early vendor SDK leakage outside allowed future adapter boundaries; verify it runs in `make quality`.
- [x] 3.4 Add `scripts/license_check.py` to verify Apache-2.0 `LICENSE`, `NOTICE`, and undeclared vendored-source baseline; verify it runs in `make license-check`.
- [x] 3.5 Add `.pre-commit-config.yaml` that points to the Phase 1 quality checks; verify `pre-commit run --all-files` can execute after environment setup.
- [x] 3.6 Add Apache-2.0 `LICENSE` and root `NOTICE`; verify `make license-check` passes.

## 4. Documentation and Verification Evidence

- [x] 4.1 Add root `README.md` covering scaffold purpose, quick start, project structure, agent app developer entrypoints, scaffold maintainer entrypoints, license/compliance, and release-process status; verify the required sections exist.
- [x] 4.2 Document forbidden dependency directions in the root README: core does not depend on templates/examples, app entrypoints do not contain business agent logic, and vendor SDKs stay behind future adapters or controlled integration modules; verify boundary text is searchable.
- [x] 4.3 Add Phase 1 unit or contract tests for package import, workspace shape, and template shell structure; verify `make test` passes.
- [x] 4.4 Run and record the Phase 1 command set: `uv sync`, `make quality`, `make test`, `make smoke-local`, `make build`, and `make license-check`; use the command results as implementation evidence before marking this change complete.
