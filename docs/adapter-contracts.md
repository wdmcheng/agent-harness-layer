# Adapter contracts

[English](adapter-contracts.md) | [简体中文](adapter-contracts.zh-CN.md)

Audience: scaffold maintainers implementing provider, queue, runtime, storage, or observability adapters, and application developers deciding which dependencies business code may use.

Navigation: [root README](../README.md) · [architecture boundaries](architecture/README.md) · [extension guide](extension-guide.md) · [security policy](security-policy.md) · [ADR-0002](adr/0002-vendor-adapter-isolation.md)

## Contract levels

| Level | Current public seam | Maintenance boundary |
|---|---|---|
| DTO | `HarnessDTO`, identity/trust DTOs, run/queue/event/eval DTOs | Serializable, validated, no SDK/ORM objects; preserve tenant/agent/run/request/trace refs across processes |
| Protocol | `ModelProvider`, `RetrievalProvider`, `EmbeddingProvider`, `RunQueue`, `EventSink`/`EventReader`, `TokenVerifier`, `PolicyProvider` | Callers depend on behavioral contracts, not concrete vendor types |
| Facade/service | `TelemetryFacade`, model/embedding invocation services, policy/approval/context services | Centralize identity, policy, budget, redaction, local-first evidence, and error semantics |
| Repository | Run, approval, audit, event, eval, retrieval, usage, and delegation repositories | Accept/return DTOs or records; isolate SQLAlchemy queries and concurrency control |
| UoW | `SQLAlchemyUnitOfWork` | Centralize transaction/commit/rollback ownership; business code never holds an `AsyncSession` |
| Adapter | `adapters/models`, `adapters/mcp`, `adapters/queue`, `adapters/runtime`, `adapters/observability`, `storage/adapters` | Controlled boundary for the corresponding vendor SDK or driver; ORM ownership also includes models, repositories, and migrations under `storage` |

Exports and protocols under `packages/agent-harness/src/agent_harness/` define the public seam. This document describes stable responsibilities; it does not promise the path or signature of every private helper.

## Invocation and data rules

1. Entry points convert untrusted HTTP/CLI input into validated DTOs and inject server-side identity. A request body cannot override tenant, reviewer, or permission.
2. Services/facades enforce permission, policy, budget, approval, workspace, and capacity gates before side effects.
3. Adapters receive requests only after prerequisite checks and return provider-neutral DTOs or closed errors. Raw SDK objects cannot cross the boundary.
4. Durable evidence is committed through a repository/UoW before degradable fan-out. Exactly-once behavior uses idempotency, claims, leases, fencing, and outbox—not in-process locks.
5. API/worker queues carry stable refs only. Workers restore execution context from PostgreSQL rather than trusting mutable objects assembled by the producer.

## Errors and degradation

- Input, permission, policy, workspace, capacity, and contract errors fail closed with structured errors before external side effects.
- Provider raw exceptions, credentials, response bodies, and host absolute paths cannot enter APIs, events, or telemetry. Retain only closed codes, bounded summaries, and safe evidence refs.
- Observability/eval provider fan-out may degrade, but failure of local database/event evidence cannot be disguised as provider degradation.
- Uncertain Redis/DBOS/PostgreSQL outcomes enter a recoverable or `needs_review` state; they are never replayed blindly.
- Approval denial creates no continuation. If enqueue after approval fails, retain a state that can be re-enqueued without replaying the handler.

## Primary adapter boundaries

### Model, embedding, and MCP

`ModelProvider`/`EmbeddingProvider` isolate vendor APIs. Current Pydantic AI and OpenAI-compatible embedding implementations live in `adapters/models/`. The MCP Python SDK lives in `adapters/mcp/python_sdk.py` and exposes only `MCPClient`/tool DTOs. Business Agents, template APIs, and eval runners do not import those SDKs directly.

Non-streaming structured output uses the separate `ModelStructuredProvider.prepare_structured()` / `PreparedStructuredModelCall.send_structured()` protocols. Each adapter send returns exactly one `StructuredProviderCandidate` and one local attempt. Adapters must not perform schema repair, stringify SDK/Pydantic objects, enable SDK retries, execute tools, or fall back to text/fake. Core `BoundModelInvocationService.complete_structured()` owns the strict schema oracle, bounded repair, joint transport×repair reservation, cleanup, durable replay, and `needs_review` fencing. Only `StructuredOutputResult` plus canonical `ModelResponse.output_text` crosses the success boundary; schema identity, attempts, usage, cost, and replay identity remain jointly durable. Structured streaming, explicit route-chain structured fallback, and tool calls are not supported.

### Queue and runtime

`RunQueue` defines enqueue/receipt/ack/claim contracts. The service profile implements Redis Streams in `adapters/queue/redis.py`; DBOS is wrapped in `adapters/runtime/dbos.py`. Recovery relies on stable message refs, owner/lease/fencing, and PostgreSQL checkpoints—not in-memory objects.

### Storage and UoW

The controlled ORM boundary includes models, repositories, and migrations under `storage`, plus engine/UoW composition in `storage/adapters/sqlalchemy.py`. Repositories encapsulate SQLAlchemy queries and concurrency control; the UoW owns transaction/commit/rollback. APIs, workers, and services compose repositories/UoW only. Business Agents, route handlers, and provider adapters never operate on sessions directly. A storage service remains a future boundary; a PostgreSQL database shared by the current API and worker is not an already-separated service.

### Event and observability

`EventSink`/`EventReader` provide local JSONL/PostgreSQL persistence and authorized reads. `TelemetryFacade` commits locally before fan-out to OTel/Logfire/Phoenix/Langfuse. SSE uses the same authorized reader rather than a new path that bypasses visibility policy.

## Import-boundary evidence

```bash
make quality
uv run python scripts/import_boundary_check.py
make test
```

Key contract tests:

- model/provider: `tests/contracts/test_model_usage_invocation_contracts.py`
- structured model/provider: `tests/contracts/test_provider_neutral_structured_public_seam_contracts.py`, `tests/contracts/test_provider_neutral_structured_transport_contracts.py`, `tests/contracts/test_provider_neutral_structured_adapter_contracts.py`
- retrieval: `tests/contracts/test_retrieval_rag_contracts.py`
- tools/MCP: `tests/contracts/test_tool_registry_public_seam_contracts.py`
- observability: `tests/contracts/test_observability_local_first_fanout_contracts.py`
- queue/runtime: `tests/contracts/test_durable_run_queue_contracts.py`, `tests/integration/test_redis_run_queue_contracts.py`
- storage/event: `tests/contracts/test_postgresql_event_sink_contracts.py`, `tests/contracts/test_usage_execution_authority_contracts.py`

Only `make smoke-service` and integration evidence prove real Redis/PostgreSQL/DBOS behavior; SQLite mocks are not substitutes.

## Common failures

- Import-boundary failure: fix dependency direction instead of adding a blanket ignore. Only adapter/integration paths may own vendor imports.
- DTO serialization failure: check for SDK/ORM objects, non-finite numbers, absolute paths, or oversized payloads.
- Invalid transaction state: confirm that the UoW owns commit/rollback and no adapter commits half a state independently.
- Provider degradation hides the main failure: first verify local repository/event commit. Main-evidence failure must remain a failure.
- Service replay duplicates a side effect: inspect idempotency key, claim owner, lease/fencing, outbox, and stable call ID rather than adding an in-process cache that hides the issue.
