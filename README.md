# Agent Harness Layer

Agent Harness Layer is a Python scaffold and core package for enterprise backend agent applications. It provides the repository shape, package boundary, verification commands, and future extension points needed to build agent services with backend engineering discipline.

The current scaffold proves the workspace, package boundary, template shell, quality commands, compliance entrypoints, typed configuration, identity context, DTO/error/trust payloads, import-boundary declarations, storage/runtime seams, and a side-effect-free doctor command. Real policy providers, tools, retrieval, observability adapters, eval gates, CI release automation, and full service-profile deployment behavior remain later changes.

## Quick Start

```bash
uv sync
make quality
make test
make smoke-local
uv run agent-harness doctor --profile local
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
- `templates/service-app` is the backend service application template. It depends on `agent-harness` through the package boundary.
- `examples` is reserved for future thin example material that validates extension points.
- `docs` is reserved for architecture, extension, adapter, security, eval/observability, release, and ADR documentation.
- `scripts` contains local quality, boundary, smoke, and compliance checks.
- `openspec` contains change contracts. It does not replace `Product-Spec.md` or `DEV-PLAN.md`.

## For Agent App Developers

Start from `templates/service-app`. Its current shell reserves:

- `app/api` for future API entrypoints
- `app/cli` for future CLI entrypoints
- `app/workers` for future worker entrypoints
- `agents/examples` for future example agents
- `configs/profiles/local.yaml` for local profile defaults
- `configs/profiles/service.yaml` for typed service-profile defaults that do not start external services by themselves
- `eval-cases/drafts` and `eval-cases/approved` for future eval data flow

The application entrypoint packages are not business agent directories. Agent logic belongs under agent-specific directories in `agents/*`.

## For Scaffold Maintainers

Maintain the package boundary first:

- `agent_harness/*` must not depend on concrete template or example code.
- `app/*` must not contain business agent logic.
- Vendor SDKs and capability libraries such as Pydantic AI, Pydantic AI Harness, DBOS, Logfire, Phoenix, and Langfuse must stay behind future adapters or controlled integration modules.
- Template app code should import settings, identity, trust, and DTO types from `agent_harness.*` instead of reading YAML or provider SDKs directly.
- `eval-cases/approved` will be written only by a future approval flow.
- Future run records must carry tenant, agent, run, and trace correlation fields once those subsystems exist.

Run `make quality` before committing. It checks formatting, linting, type checking, and import boundaries.

Run `uv run agent-harness doctor --profile local` to verify the selected profile can be loaded through the packaged CLI seam. The command reports profile, storage, queue, observability, policy, identity, and model status without opening database, queue, model, or observability connections.

## P0 Deployment Boundaries

P0 keeps the service-app profile deployable as a backend template without pretending it is already a distributed system. The current local profile is single-process. The typed service profile records the future API process and runtime worker split, shared storage/queue settings, and provider-neutral boundaries.

Future split paths are:

- Access/API gateway: owns HTTP/CLI entrypoints, auth injection, request/response schemas, and input guardrails.
- Runtime worker: owns run lifecycle, checkpoint/resume, and HITL continuation once policy support exists.
- Model gateway and tool gateway: own provider/tool SDK imports behind adapter boundaries.
- Storage service: owns repositories, migrations, and transaction boundaries.
- Event/observability pipeline: owns CanonicalEvent, trace, audit, eval, and provider adapters.

Cross-boundary data should move through Pydantic DTOs, context refs, identity/permission context, provider facades, repository interfaces, or later CanonicalEvent contracts. Do not pass raw ORM sessions, provider SDK objects, or mutable process globals across these boundaries.

## Deep Docs

Deep documentation is introduced incrementally:

- `docs/architecture/README.md` contains architecture diagram sources, PNG previews, and deployment boundary notes.
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

The current codebase does not vendor third-party source. Do not copy third-party code into the repository without recording source, license, and modification details.

## Release Process

Release automation is not implemented yet. The core package can be built locally with:

```bash
make build
```

Future changes will add semantic versioning, changelog generation, release artifacts, and private registry publishing.
