# ADR-0002: Isolate vendor SDKs at adapter/integration boundaries

[English](0002-vendor-adapter-isolation.md) | [简体中文](0002-vendor-adapter-isolation.zh-CN.md)

- Status: Accepted
- Date: 2026-07-20
- Related: [ADR-0001](0001-p0-service-boundaries.md) · [adapter contracts](../adapter-contracts.md) · [extension guide](../extension-guide.md)

## Context

The Agent runtime touches model, embedding, MCP, durable-execution, and observability providers. Direct dependencies on SDKs such as Pydantic AI, DBOS, Logfire, Phoenix, or Langfuse from business Agents, the template app, or core DTOs would spread upstream changes across the repository. Cross-process messages would also carry non-serializable or secret-bearing vendor objects, creating migration resistance when tool/model gateways and the event pipeline are physically separated.

## Decision

1. Core callers depend only on provider-neutral Pydantic DTOs, protocols, facades, repositories/UoW, and `CanonicalEvent`.
2. Vendor SDK imports are allowed only under `packages/agent-harness/src/agent_harness/adapters/` or a controlled integration path explicitly approved by `contracts/boundaries.py`.
3. `agents/*`, `templates/service-app/app/*`, eval runners, and core business services do not import vendor SDKs. The composition root injects concrete adapters into public seams.
4. Adapters convert SDK requests/responses/exceptions into public DTOs, closed errors, and redacted evidence. SDK objects, raw responses, credentials, host absolute paths, and uncontrolled payloads do not cross the boundary.
5. Local/fake adapters remain available so core contracts, eval, and smoke do not require a real model key or SaaS provider.
6. A new vendor first adds a dependency pin/extra, adapter contract, import-boundary rule, redaction/degradation tests, and release/license review. A direct import alone is not sufficient.

## Alternatives

- Call SDKs directly from business code: less boilerplate, but upgrade, testing, redaction, policy, and future separation costs spread everywhere. Rejected.
- Create one “universal vendor DTO”: apparent uniformity would elevate private SDK concepts into core contracts. Rejected.
- Physically separate every provider in P0: out of scope and adds network/deployment complexity before contracts stabilize. Keep logical isolation first. Rejected.

## Consequences

- A provider requires an explicit adapter and contract tests, increasing initial code but keeping callers and cross-process DTOs stable.
- Provider features are usable only when mapped into public contracts or introduced through a controlled capability seam; they cannot pass through without boundaries.
- Provider failures may degrade under contract. Failure of local durable evidence remains the main failure and cannot be disguised as provider degradation.
- Future model/tool gateway and event-pipeline separation can move adapters along existing seams without rewriting business Agents.

## Evidence

```bash
make quality
uv run python scripts/import_boundary_check.py
make test
```

Implementations include `adapters/models/`, `adapters/mcp/`, `adapters/runtime/`, and `adapters/observability/`. Contract evidence includes `tests/contracts/test_agent_registry_router_model_contracts.py`, `tests/contracts/test_observability_provider_adapters_contracts.py`, and `tests/contracts/test_tool_registry_public_seam_contracts.py`.

## Review triggers

- A new vendor SDK or capability library enters dependencies.
- An existing SDK has a breaking upgrade, license change, or no longer maps to public DTOs.
- Physical tool/model gateway or event-pipeline separation begins.
- The import boundary needs an exception, or a raw provider object would have to cross a process.

After a trigger, create a new behavior/architecture change contract first. Do not relax the import checker directly.
