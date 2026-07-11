# Agent Harness Layer

Agent Harness Layer is a Python scaffold and core package for enterprise backend agent applications. It provides the repository shape, package boundary, verification commands, and future extension points needed to build agent services with backend engineering discipline.

The current scaffold proves the workspace and package boundary, a copyable FastAPI/CLI/worker service app, typed configuration and identity, durable run and approval continuation, policy-controlled tools, retrieval, observability adapters, the base trace-to-eval gate, four runnable example agents, a safe agent scaffold command, and a Compose service profile with physically separate API and runtime-worker processes. Deep maintainer documentation and CI/release automation remain later phases.

## Quick Start

```bash
uv sync
make quality
make test
make smoke-local
make eval
# Requires Docker Compose, PostgreSQL, and Redis:
make smoke-service
uv run agent-harness doctor --profile local
uv run agent-harness agents list
make build
make license-check
```

`templates/service-app` uses the `local` profile and fake/local provider defaults. Real model keys and external observability providers are not required for local smoke or doctor checks.

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

- `packages/agent-harness` is the buildable core package. It exposes public configuration, identity, DTO, error, trust, and boundary contracts. It must not depend on `templates/*` or `examples/*`.
- `templates/service-app` is the backend service application template. It depends on `agent-harness` through the package boundary and contains the runnable P0 examples under `agents/examples`.
- The root `examples` directory remains reserved for package-level examples; the maintained service-app examples live with the template they exercise.
- `docs` is reserved for architecture, extension, adapter, security, eval/observability, release, and ADR documentation.
- `scripts` contains local quality, boundary, smoke, and compliance checks.
- `openspec` contains change contracts. It does not replace `Product-Spec.md` or `DEV-PLAN.md`.

## For Agent App Developers

Start from `templates/service-app`. It currently provides:

- `app/api` routes for health, agents, runs, policies, approvals, and eval operations
- `app/cli` with the app-specific Uvicorn `serve` entrypoint
- `app/workers` with a runtime worker that shares the core composition seam
- `agents/examples` with RAG assistant, ticket triage, repo analyst, and dev assistant flows
- `configs/profiles/local.yaml` for local profile defaults
- `configs/profiles/service.yaml` plus a wheel-only Docker Compose image for PostgreSQL, Redis, migration, API, and runtime worker
- `eval-cases/drafts` and `eval-cases/approved` for the human-reviewed trace-to-eval flow

The application entrypoint packages are not business agent directories. Agent logic belongs under agent-specific directories in `agents/*`.

The template Makefile exposes `run-rag`, `run-ticket`, `run-repo`, `run-dev`, `eval`, `smoke-local`, and `smoke-service`. Use `agent-harness scaffold agent <agent_id>` from a copied service-app root to generate a validated, no-tool-permission Agent package; review its draft eval case before moving it into the approved dataset.

## For Scaffold Maintainers

Maintain the package boundary first:

- `agent_harness/*` must not depend on concrete template or example code.
- `app/*` must not contain business agent logic.
- Vendor SDKs and capability libraries such as Pydantic AI, Pydantic AI Harness, DBOS, Logfire, Phoenix, and Langfuse must stay behind adapters or controlled integration modules.
- Template app code should import settings, identity, trust, and DTO types from `agent_harness.*` instead of reading YAML or provider SDKs directly.
- `eval-cases/approved` is written only by the approval flow; automatic trace detectors may create drafts, but must not write approved cases directly.
- Run, approval, tool, trace, and eval evidence must retain the applicable tenant, agent, run, request, and trace correlation fields.

Run `make quality` before committing. It checks formatting, linting, type checking, and import boundaries.

Run `uv run agent-harness doctor --profile local` to verify the selected profile can be loaded through the packaged CLI seam. The command reports profile, storage, queue, observability, policy, identity, and model status without opening database, queue, model, or observability connections.

## P0 Deployment Boundaries

P0 keeps the service-app profile deployable as a backend template without pretending every logical boundary is already a microservice. The local profile is single-process. The service profile now runs PostgreSQL, Redis, migration, FastAPI, and the runtime worker as separate Compose services. `make smoke-service` copies the template outside the workspace, installs only the built core wheel, then proves authenticated HTTP enqueue, Redis receipt fencing and reclaim, DBOS hard-crash recovery, shared PostgreSQL checkpoint/event evidence, approval continuation, deny-without-continuation, and scoped cleanup.

Future split paths are:

- Access/API gateway: owns HTTP/CLI entrypoints, auth injection, request/response schemas, and input guardrails.
- Runtime worker: currently owns run lifecycle, checkpoint/resume, DBOS recovery, and HITL continuation as a separate service-profile process.
- Model gateway and tool gateway: own provider/tool SDK imports behind adapter boundaries.
- Model/tool gateways are the next physical split after the worker; they retain provider/tool SDK imports behind adapters.
- Event/observability pipeline is split after those gateways and owns CanonicalEvent fan-out, trace, audit, eval, and provider adapters.
- Storage service remains a future boundary until repository contracts are stable; PostgreSQL is currently shared directly through repository/UoW interfaces.

Cross-boundary data should move through Pydantic DTOs, context refs, identity/permission context, provider facades, repository interfaces, or later CanonicalEvent contracts. Do not pass raw ORM sessions, provider SDK objects, or mutable process globals across these boundaries.

## Deep Docs

Deep documentation is introduced incrementally:

- `docs/architecture/README.md` contains architecture diagram sources, PNG previews, and deployment boundary notes.
- `docs/extension-guide.md` is reserved for extension guidance.
- `docs/adapter-contracts.md` is reserved for adapter contracts.
- `docs/eval-observability-loop.md` records the implemented trace-to-eval foundation and eval experiment / harness acceptance loop; the corresponding OpenSpec changes are archived.
- `docs/security-policy.md` is reserved for security and HITL policy.
- `docs/release-process.md` is reserved for release automation.
- `docs/adr/` is reserved for architecture decisions.

## License & Compliance

This project is licensed under Apache-2.0. The root `LICENSE` file is the license text, and `NOTICE` is the place for required third-party notices and source attributions.

Run:

```bash
make license-check
```

The current codebase does not vendor third-party source. Do not copy third-party code into the repository without recording source, license, and modification details.

## Release Process

Release automation is not implemented yet. The core package can be built locally with:

```bash
make build
```

Future changes will add semantic versioning, changelog generation, release artifacts, and private registry publishing.
