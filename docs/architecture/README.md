# Architecture and deployment boundaries

[English](README.md) | [简体中文](README.zh-CN.md)

Audience: application developers who need to understand current runtime boundaries, and scaffold maintainers responsible for package, template, and adapter contracts.

Navigation: [root README](../../README.md) · [Build an Agent](../building-an-agent.md) · [engineering principles](../engineering-principles.md) · [extension guide](../extension-guide.md) · [adapter contracts](../adapter-contracts.md) · [context/trust boundaries](../context-and-trust-boundary.md) · [security policy](../security-policy.md) · [ADR](../adr/0001-p0-service-boundaries.md)

This directory is the fixed home for project architecture diagrams. Diagrams explain relationships; current behavior is still governed by `Product-Spec.md`, `API-Contract.md`, public DTOs/protocols, production code, and tests. A diagram cannot replace evidence when it conflicts with code.

## File responsibilities

| Diagram | Editable source | PNG preview | Purpose |
|---|---|---|---|
| Enterprise Pydantic AI cybernetic full-stack architecture | `pydantic-ai-agent-architecture.drawio` / `pydantic-ai-agent-architecture.excalidraw` | `pydantic-ai-agent-architecture.png` | Product overview of the five-layer runtime, Agent Loop, governance/observability planes, and separable P0 deployment boundaries |
| Agent Harness Layer technical architecture | `agent-harness-technical-architecture.drawio` / `agent-harness-technical-architecture.excalidraw` | `agent-harness-technical-architecture.png` | Development view of the core package, template app, DTOs, `CanonicalEvent`, repository/UoW, and provider/facade boundaries |
| Agent Harness Layer runtime and trust boundaries | `agent-harness-runtime-trust-boundaries.drawio` / `agent-harness-runtime-trust-boundaries.excalidraw` | `agent-harness-runtime-trust-boundaries.png` | Runtime flow across CLI/API, `RunOrchestrator`, storage/checkpoint, EventBus/artifacts, and untrusted input |
| Agent Harness Layer deployment boundaries | `agent-harness-deployment-boundaries.drawio` / `agent-harness-deployment-boundaries.excalidraw` | `agent-harness-deployment-boundaries.png` | Local/service profiles, API/worker/PostgreSQL/Redis cooperation, and future separation |

## Start with five layers and two wings

The diagrams answer where capabilities belong and how boundaries connect; they do not require business developers to rewrite every layer. The executable route, files changed per layer, wing integration, and complete `support.triage` mapping are in [Build an Agent with five layers and two wings](../building-an-agent.md).

When reading the product overview, remember:

- `Graph Nodes`, `GraphState`, complex long-term memory, and independent tool/model gateways are target or future extension points, not prerequisites for the first Agent.
- The diagram's `@agent.tool Registry` is a conceptual tool-registration label. The public interface is the typed `ToolRegistry` plus descriptor/result DTOs; there is no public decorator that bypasses registry, policy, workspace, or approval.

## Current deployment boundaries

- The local profile remains a single-process SQLite/in-memory/local-JSONL development topology.
- The service profile currently consists of PostgreSQL, Redis, migration, FastAPI API, and a runtime worker. The API dispatches run creation and approval continuation through `RunQueue`; query, validation, and denial control-plane operations stay in the API. The worker owns a stable DBOS executor ID and handles run/checkpoint/approved continuation.
- API and worker exchange queue DTOs, repository DTOs, and `CanonicalEvent` refs only. `source_ref`, `trust_level`, context trace, guardrail/audit, and tenant/run/request/trace correlation must survive the boundary.
- The future physical split order is runtime worker (already separate) → tool/model gateway → observability/event pipeline. A storage service comes only after repository contracts stabilize. Purple dashed lines still indicate future boundaries, not current Compose services.

### Current and future ownership

| Boundary | Current fact | Invariant for extension | Not implemented |
|---|---|---|---|
| Access/API | FastAPI/CLI inject auth, validate requests, convert DTOs; the service-profile API only enqueues runs | Identity, permission, and request/trace/run correlation enter stable DTOs | Standalone API gateway product |
| Runtime worker | Separate process in the service profile; owns DBOS executor, run/checkpoint, and approval continuation | Queue carries stable refs only; restore from PostgreSQL truth | Multi-executor coordination |
| Model/tool | Called in-process through provider, registry, policy, and facade seams | Vendor SDKs stay in adapter/integration boundaries | Standalone model/tool gateways |
| Event/observability | `CanonicalEvent` writes to local/PostgreSQL sink before optional provider fan-out | Provider failure cannot delete local evidence; visibility uses authorized readers | Standalone event pipeline |
| Storage | API/worker share PostgreSQL; local uses SQLite | Business layers depend on repository/UoW and never pass `AsyncSession` | Standalone storage service |

See [ADR-0001](../adr/0001-p0-service-boundaries.md) for deployment separation, [ADR-0002](../adr/0002-vendor-adapter-isolation.md) for vendor isolation, [ADR-0003](../adr/0003-redis-runtime-license-policy.md) for Redis runtime and license-review boundaries, and [ADR-0004](../adr/0004-swagger-ui-offline-assets.md) / [ADR-0005](../adr/0005-redoc-offline-assets.md) for the copied template's offline API-documentation assets.

## Validation and evidence

```bash
make quality       # format/lint/type/import boundary
make test          # unit, contract, offline integration
make smoke-local   # SQLite/in-memory/fake model/local JSONL
make smoke-service # requires Docker Compose; real PostgreSQL/Redis/API/worker
```

Only `make smoke-service` proves the real queue, cross-process recovery, and PostgreSQL event evidence. `make smoke-local` cannot replace it. HTTP/SSE contract evidence is under `tests/contracts/`; real Redis/PostgreSQL/DBOS contracts are under `tests/integration/`; service-level scripts are under `templates/service-app/scripts/`.

Troubleshooting: if diagrams disagree with code, first check whether the diagram still marks the boundary as future. If API health passes but runs do not advance, inspect migration, Redis consumer group, and worker logs. If local passes while service fails, inspect Docker, secret files, PostgreSQL migration, and Redis namespace rather than downgrading the problem to SQLite validation.

## Maintenance rules

- `.drawio` is the primary editable source; `.excalidraw` supports collaborative editing; `.png` is a review preview only.
- A semantic diagram change updates only the controlling sources it actually affects: `Product-Spec.md` for product behavior, `API-Contract.md` for a changed public API/CLI/module contract, and `DEV-PLAN.md` for changed phase ownership, files, evidence, or state. Do not touch an unaffected contract merely to keep timestamps aligned.
- If a diagram says “to be implemented in Phase N,” completion of that phase must update both the diagram source and PNG preview—not only code and plans.
- A boundary change first updates the applicable product/API/OpenSpec contract, then the diagram source and exported PNG. The PNG is not editable truth.
- An architecture or cross-module change follows the [engineering principles](../engineering-principles.md): identify the invariant and variation axis, update applicable contracts before code, create a red contract, and put mechanically decidable rules in checker/CI.
