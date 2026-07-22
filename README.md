# Agent Harness Layer

## What this scaffold is

Agent Harness Layer is a Python scaffold and core package for enterprise backend agent applications. It provides the repository shape, package boundary, verification commands, and future extension points needed to build agent services with backend engineering discipline.

The current scaffold proves the workspace and package boundary, a copyable FastAPI/CLI/worker service app, typed configuration and identity, durable run and approval continuation, policy-controlled tools, retrieval, observability adapters, the trace-to-eval gate, four runnable example agents, a safe agent scaffold command, and a Compose service profile with physically separate API and runtime-worker processes. The maintainer documentation is available under `docs/`. Phase 15 now includes repository-local GitHub/GitLab pipeline definitions, release preview and protected promotion seams, and a versioned compliance report; hosted runner execution and real promotion remain explicitly unverified and are not performed by this checkout.

## Quick Start

```bash
uv sync
make quality
make test
make smoke-local
make eval
# Requires Docker Compose, PostgreSQL, and Redis:
make smoke-service
make build
make license-check
```

`make smoke-local` creates isolated local state, injects an ephemeral budget fingerprint key, runs the required migration, and exercises the packaged CLI. `templates/service-app` uses fake/local provider defaults, so real model keys and external observability providers are not required. Direct `doctor`、`agents list`、run 或 `make dev` calls still require a stable fingerprint key and migrated local database; follow the [service-app Quick Start](templates/service-app/README.md#quick-start) instead of invoking them against an uninitialized profile.

| Command | Profile / prerequisite | Evidence boundary |
|---|---|---|
| `make quality` | local toolchain | format、lint、type 和 import boundary |
| `make test` | local dependency set | unit、contract 和离线 integration |
| `make eval` | approved fake-model cases | reviewed eval cases；不代表生产模型质量 |
| `make smoke-local` | SQLite/in-memory/fake model | local runtime；不替代 service profile |
| `make smoke-service` | Docker Compose、PostgreSQL、Redis | wheel-only API/worker 跨进程和恢复证据 |
| `make build` | uv build | 本地 wheel/sdist；不发布 |
| `make license-check` | repository files plus `uv.lock` and `compliance/third-party.toml` | fail-closed license inventory/report、NOTICE、vendoring 和 pinned service image identity；不是法律意见或完整 SBOM |

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
- `docs` contains the architecture, extension, adapter, context/trust, security, eval/observability, release-boundary, and ADR documentation.
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

Use the following maintainer guides while extending a copied app:

- [Extension guide](docs/extension-guide.md): supported seams for agents, tools, models, retrieval, observability, and eval.
- [Context and trust boundary](docs/context-and-trust-boundary.md): untrusted input, context assembly, guardrails, HITL, and event return paths.
- [Security policy](docs/security-policy.md): identity, permissions, policy, approvals, workspace access, secrets, and audit evidence.
- [Service-app guide](templates/service-app/README.md): copy/bootstrap, local profile, service profile, and runnable examples.

The template Makefile exposes `run-rag`, `run-ticket`, `run-repo`, `run-dev`, `eval`, `smoke-local`, and `smoke-service`. After completing the copied app's local initialization, use `uv run agent-harness scaffold agent support.triage` to generate a validated, no-tool-permission Agent package; review its draft eval case before moving it into the approved dataset.

## For Scaffold Maintainers

Maintain the package boundary first:

- `agent_harness/*` must not depend on concrete template or example code.
- `app/*` must not contain business agent logic.
- Vendor SDKs and capability libraries such as Pydantic AI, Pydantic AI Harness, DBOS, Logfire, Phoenix, and Langfuse must stay behind adapters or controlled integration modules.
- Template app code should import settings, identity, trust, and DTO types from `agent_harness.*` instead of reading YAML or provider SDKs directly.
- `eval-cases/approved` is written only by the approval flow; automatic trace detectors may create drafts, but must not write approved cases directly.
- Run, approval, tool, trace, and eval evidence must retain the applicable tenant, agent, run, request, and trace correlation fields.
- Multi-agent delegation must go through `AgentRegistry` and `PolicyEngine`; callers must not invoke a child executor directly or bypass tenant, cycle/depth, budget, permission, and approval checks.

Run `make quality` before committing. It checks formatting, linting, type checking, and import boundaries.

Run `make smoke-local` to verify the packaged local CLI seam with isolated state. After completing the [service-app local initialization](templates/service-app/README.md#quick-start), direct `doctor` reports profile, storage, queue, observability, policy, identity, model, and migration status without opening external queue, model, or observability connections.

The detailed maintainer contracts are [architecture](docs/architecture/README.md), [adapter contracts](docs/adapter-contracts.md), [eval and observability](docs/eval-observability-loop.md), [release process](docs/release-process.md), and the [current ADRs](docs/adr/0001-p0-service-boundaries.md). `make quality` enforces core/template/import boundaries; vendor integrations are only allowed behind the locations documented in the adapter contracts.

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

| Maintainer question | Contract |
|---|---|
| What runs today, and what may split later? | [Architecture and deployment boundaries](docs/architecture/README.md) |
| Where can I add an agent or capability? | [Extension guide](docs/extension-guide.md) |
| Which DTO, protocol, facade, repository, and UoW boundaries must stay stable? | [Adapter contracts](docs/adapter-contracts.md) |
| How are untrusted inputs assembled, governed, approved, and returned? | [Context and trust boundary](docs/context-and-trust-boundary.md) |
| How do auth, permissions, policy, workspace controls, secrets, and audit interact? | [Security policy](docs/security-policy.md) |
| How do traces become reviewed eval evidence? | [Eval and observability loop](docs/eval-observability-loop.md) |
| What can be verified locally, and what remains hosted/unverified? | [Release process](docs/release-process.md) |
| Why were the service, vendor, and Redis boundaries chosen? | [ADR-0001](docs/adr/0001-p0-service-boundaries.md), [ADR-0002](docs/adr/0002-vendor-adapter-isolation.md), [ADR-0003](docs/adr/0003-redis-runtime-license-policy.md) |

All command examples in these guides are either runnable in this checkout or explicitly marked as requiring the Docker Compose service profile. They distinguish repository-controlled dependency versions, Compose image tags, external CLI versions observed during verification, and runtime versions reported by a concrete smoke run.

## License & Compliance

This project is licensed under Apache-2.0. The root `LICENSE` file is the license text, and `NOTICE` is the place for required third-party notices and source attributions.

Run:

```bash
make license-check
```

The current codebase does not vendor third-party source. The runtime dependency inventory and policy are tracked in [`compliance/third-party.toml`](compliance/third-party.toml); do not copy third-party code into the repository without recording source, license, and modification details there and in the required ADR/NOTICE entries.

## Release Process

The current checkout supports manual quality, test, eval, local/service smoke, build, license verification, CI-contract validation, and release dry-run. The core package can be built locally with:

```bash
make build
```

This creates local wheel/sdist artifacts and `dist/SHA256SUMS`; it does not publish them. `make release-dry-run` creates an ignored `release-preview/v1` manifest for releasable and no-release histories without changing Git history or a registry. Promotion and private-registry publishing are protected plan-only-by-default seams; hosted GitHub/GitLab execution, external provider calls, and real publish remain unverified. See the [release process](docs/release-process.md) for the exact gate and evidence boundary.

The current Phase 15 candidate passed the real local PostgreSQL/Redis service smoke with the repository-pinned uv `0.11.29` and localhost excluded from the host proxy. Fresh Reviewer 1 passed the four scoped fixes; the owner explicitly waived the final Reviewer 2/3 gate for this Phase without claiming reviewer PASS. All three OpenSpec changes are locally `ready-to-archive` and remain active. Hosted execution is still unverified.
