# Context and trust boundaries

[English](context-and-trust-boundary.md) | [简体中文](context-and-trust-boundary.zh-CN.md)

Audience: application developers orchestrating Agent loops, retrieval, tools, approvals, and event delivery; scaffold maintainers responsible for trust DTOs, guardrails, policy, and runtime seams.

Navigation: [root README](../README.md) · [architecture](architecture/README.md) · [adapter contracts](adapter-contracts.md) · [security policy](security-policy.md) · [Eval/Observability](eval-observability-loop.md)

## Current flow

```text
HTTP/CLI input (untrusted)
  -> auth identity + permission
  -> InputGuardrail / PolicyEngine
  -> ContextInput + SourceRef/ContextRef
  -> retrieval/tool/history fragments
  -> ContextAssembler (budget, priority, truncation, trace)
  -> model/tool invocation
  -> output guard / policy / HITL
  -> CanonicalEvent + audit + checkpoint
  -> authorized EventReader
  -> CLI JSON stream or HTTP SSE
```

In the service profile, the API authenticates, validates, and enqueues; the runtime worker restores execution context from PostgreSQL and executes the loop. Redis messages carry stable refs only. The local profile may run in one process, but that does not relax trust semantics.

## Trust objects and invariants

- `SourceRef` identifies the source. `ContextRef` associates source, trust level, and logical evidence.
- `TrustLevel` is a closed enum; free text cannot manufacture a higher trust level. External input, retrieval, and tool output must be classified by their actual source.
- `ContextInput`/`ContextOutput` are cross-layer DTOs. They cannot contain raw provider objects, ORM models, credentials, or host absolute paths.
- `ContextAssembler` deterministically orders fragments, applies token budgets, drops history or truncates retrieval/tool results, and emits `ContextFragmentTrace`/`ContextAssemblyResult`. Dropped or truncated content must leave a decision trace; it cannot disappear silently.
- Tenant, Agent, run, request, trace, and applicable parent/delegation refs remain in durable evidence. API/worker boundaries, approval resume, and SSE resume cannot change identity.

## Handling untrusted input

1. HTTP/CLI bodies contain business input only. Trusted entry points inject tenant, reviewer, permission, and service identity.
2. Enforce schema, input guardrail, permission, policy, workspace, and budget/capacity checks before retrieval, model, tool, filesystem, or shell side effects.
3. Retrieved documents, web pages, MCP/tool output, and history may contain prompt injection. They are data with source/trust refs, not system instructions, and cannot override policy, tool allowlists, or approval requirements.
4. Before output enters events, telemetry, or APIs, apply secret redaction, size/finite-number validation, and visibility classification. Use a controlled artifact ref for large evidence instead of truncating it and claiming it is complete.
5. Input whose source, tenant, or trust level cannot be proved fails closed; the system never restores service by defaulting it to trusted.

## Guardrail, policy, and HITL feedback

Input guardrails validate input shape and explicit refusal. `PolicyEngine` returns allow, deny, or require-approval from identity, permission, Agent/tool/action, and configuration. Require-approval persists the request, pauses the run, and resumes from checkpoint/continuation only after approval. Denial creates terminal evidence without a continuation.

Approval is not permanent authority. Before a side effect, `ApprovedToolExecutor` revalidates the grant, argument hash, tenant/run/tool, lease, and execution state. A lost lease or uncertain result enters a recoverable/human-review boundary; the executor does not rerun “just to see.” See the [security policy](security-policy.md) for complete permission and secret rules.

## Event delivery

- Implemented today: authorized `EventReader`, CLI `events stream --after-seq`, and HTTP `GET /api/v1/runs/{run_id}/events/stream` SSE.
- Cursor semantics: CLI `--after-seq` and HTTP `Last-Event-ID` both resume exclusively; terminal is followed by EOF.
- Visibility: readers return public events by default. Internal events require the same authorization policy. Cross-tenant and missing resources converge to prevent enumeration.
- Reads have no side effects. Resume, disconnects, and slow clients cannot create events, modify runs, or prefetch unbounded pages.
- WebSocket is a future P1 capability. There is currently no endpoint, protocol, or deployment entry; an SSE client cannot be documented as “WebSocket supported.”

## Validation and evidence

```bash
make test
make smoke-local
# Real API/worker/PostgreSQL/Redis, approval continuation, and SSE resume:
make smoke-service
```

Key evidence: `tests/contracts/test_auth_policy_hitl_policy_contracts.py`, `tests/contracts/test_auth_policy_hitl_event_contracts.py`, `tests/contracts/test_retrieval_rag_contracts.py`, `tests/contracts/test_sse_authorized_reader_contracts.py`, `tests/contracts/test_sse_event_reader_postgresql_contracts.py`, and `templates/service-app/scripts/service_approval_smoke.py`.

Troubleshooting: when context differs from expectation, inspect assembly trace ordering, budget, and drop/truncate decisions. If approval does not resume, inspect durable resolution, queue re-enqueue, and worker owner. For SSE 401/403/404, inspect token/permission/tenant rather than bypassing the reader. For missing or duplicate events, inspect stable event IDs, sequence reservation, and the terminal invariant.
