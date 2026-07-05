# Agent Harness Layer

Agent Harness Layer is a Python scaffold and core package for enterprise backend agent applications. It provides the repository shape, package boundary, verification commands, and future extension points needed to build agent services with backend engineering discipline.

Phase 1 is intentionally small. It proves the workspace, package boundary, template shell, quality commands, and compliance entrypoints. Runtime orchestration, storage, policy, tools, retrieval, observability, eval, CI release automation, and service-profile Docker behavior are later changes.

## Quick Start

```bash
uv sync
make quality
make test
make smoke-local
make build
make license-check
```

`templates/service-app` uses the `local` profile and fake/local provider defaults in Phase 1. Real model keys and external observability providers are not required.

## Project Structure

```text
project/
├── packages/
│   └── agent-harness/
├── templates/
│   └── service-app/
├── examples/
├── docs/
├── scripts/
├── openspec/
├── pyproject.toml
├── uv.lock
├── Makefile
├── LICENSE
├── NOTICE
└── README.md
```

- `packages/agent-harness` is the buildable core package. It must not depend on `templates/*` or `examples/*`.
- `templates/service-app` is the backend service application template. It depends on `agent-harness` through the package boundary.
- `examples` is reserved for future thin example material that validates extension points.
- `docs` is reserved for architecture, extension, adapter, security, eval/observability, release, and ADR documentation.
- `scripts` contains local quality, boundary, smoke, and compliance checks.
- `openspec` contains change contracts. It does not replace `Product-Spec.md` or `DEV-PLAN.md`.

## For Agent App Developers

Start from `templates/service-app`. Its current Phase 1 shell reserves:

- `app/api` for future API entrypoints
- `app/cli` for future CLI entrypoints
- `app/workers` for future worker entrypoints
- `agents/examples` for future example agents
- `configs/profiles/local.yaml` for local profile defaults
- `eval-cases/drafts` and `eval-cases/approved` for future eval data flow

The application entrypoint packages are not business agent directories. Agent logic belongs under agent-specific directories in `agents/*`.

## For Scaffold Maintainers

Maintain the package boundary first:

- `agent_harness/*` must not depend on concrete template or example code.
- `app/*` must not contain business agent logic.
- Vendor SDKs such as Pydantic AI, DBOS, Logfire, Phoenix, and Langfuse must stay behind future adapters or controlled integration modules.
- `eval-cases/approved` will be written only by an approval flow in later phases.
- Future run records must carry tenant, agent, run, and trace correlation fields once those subsystems exist.

Run `make quality` before committing. It checks formatting, linting, type checking, and import boundaries.

## Deep Docs

Deep documentation is introduced incrementally:

- `docs/architecture.md` is reserved for architecture and deployment boundaries.
- `docs/extension-guide.md` is reserved for extension guidance.
- `docs/adapter-contracts.md` is reserved for adapter contracts.
- `docs/eval-observability-loop.md` is reserved for trace-to-eval guidance.
- `docs/security-policy.md` is reserved for security and HITL policy.
- `docs/release-process.md` is reserved for release automation.
- `docs/adr/` is reserved for architecture decisions.

## License & Compliance

This project is licensed under Apache-2.0. The root `LICENSE` file is the license text, and `NOTICE` is the place for required third-party notices and source attributions.

Run:

```bash
make license-check
```

Phase 1 does not vendor third-party source. Do not copy third-party code into the repository without recording source, license, and modification details.

## Release Process

Release automation is not implemented in Phase 1. The core package can be built locally with:

```bash
make build
```

Future changes will add semantic versioning, changelog generation, release artifacts, and private registry publishing.

