# Security policy and incident response

[English](security-policy.md) | [简体中文](security-policy.zh-CN.md)

Audience: application developers configuring service-app security boundaries, and scaffold maintainers responsible for auth, policy, approval, workspace, secrets, and audit seams.

Navigation: [root README](../README.md) · [context/trust boundaries](context-and-trust-boundary.md) · [adapter contracts](adapter-contracts.md) · [architecture boundaries](architecture/README.md) · [release boundaries](release-process.md)

## Identity, authentication, and permissions

- The local profile uses an explicit local identity for offline development only; it is not production authentication.
- The service profile verifies HTTP Bearer/API keys. Plaintext tokens exist only at request entry; the database stores hashes. `TokenVerifier` resolves tenant, subject, and permissions; request bodies cannot override them.
- Endpoints verify identity and permission before resource access. Cross-tenant and missing resources use consistent external semantics to prevent ID enumeration.
- Delegation passes `AgentRegistry`, identity/permission, policy, cycle/depth/budget, and tenant boundaries. Callers cannot invoke a child executor directly.

## Policy and approval

`PolicyEngine` returns allow, deny, or require-approval. Dangerous tool/model/delegation operations check policy before side effects. Require-approval persists the request and a safe summary, pauses the run, and uses server-side reviewer identity for approve/deny with an audit record.

- Denial creates no continuation.
- Approval authorizes one controlled execution matching tenant/run/tool/action/argument hash; the executor revalidates grant and lease.
- If enqueue after approval fails, retain a re-enqueueable state. Retry enqueues the continuation only; it does not replay the handler.
- `needs_review` or an uncertain outcome requires human review of durable evidence and never triggers an automatic rerun.

## Workspace and tools

- `WorkspacePolicy` fails closed on readable/writable roots, normalized paths, and symlink escape.
- File/shell tools do not inherit the full host environment. Only allowlisted variables pass through, and path arguments must remain in the workspace.
- Tools declare schema, permission, and policy action in the registry. There is no “temporarily execute it directly” bypass.
- Tool output passes `guarded_tool_payload`, secret-pattern detection, size gates, and artifact handling before entering events or model context.

## Secret loading and redaction

- Do not commit `.env`, tokens, DSN passwords, provider keys, temporary credentials, or generated state. The template `.gitignore` prevents local-state commits; `.env.example` documents fields only.
- Sensitive service-profile configuration uses read-only regular files under a trusted secret root. Reject symlinks, directories, files over 64 KiB, direct/file conflicts, and empty or invalid content.
- Configuration, doctor, health, logs, API errors, events, telemetry, eval evidence, and artifacts use unified redaction. Raw Pydantic error chains and traceback locals cannot escape.
- Provider raw responses never enter public errors. If a redacted value is still oversized or structurally unsafe, retain a safe summary and logical evidence ref only.

## Event visibility and audit

`CanonicalEvent` is run evidence, not universal visibility. Readers return public events by default; internal evidence needs additional authority. Related evidence retains tenant, Agent, run, request, trace, and action/policy/approval refs. Audit records decisions and actors, not secrets. Provider fan-out failure cannot delete local audit/event evidence.

There is currently no event-retention/TTL job. A future cleanup capability must define cursor expiration, compliance retention, and deletion audit explicitly; operations scripts cannot delete evidence silently.

## Security validation

```bash
make quality
make test
make smoke-local
# Requires Docker Compose; proves real auth, secret files, Redis/PostgreSQL,
# approval recovery, and log redaction:
make smoke-service
make license-check
```

Key evidence: `tests/contracts/test_auth_policy_hitl_openapi_contracts.py`, `tests/contracts/test_auth_policy_hitl_policy_contracts.py`, `tests/contracts/test_approval_resolution_forgery_contracts.py`, `tests/contracts/test_tool_registry_authorization_contracts.py`, `tests/contracts/test_sse_authorized_reader_contracts.py`, and `tests/contracts/test_observability_local_first_fanout_contracts.py`.

## Incident response

1. Stop the affected entry point or provider fan-out first, while preserving PostgreSQL/event/audit/claim evidence. Do not clear databases or replay blindly.
2. Correlate auth, policy, approval, tool/model usage, and terminal events by tenant/run/request/trace; identify pending leases or uncertain side effects.
3. If secret leakage is suspected, rotate the credential and inspect health/doctor/log/API/event/artifact/provider payloads. Never copy the original value into an issue or test fixture.
4. For workspace denial, inspect normalized paths and symlinks. For permission denial, inspect server-side identity/permission. For stuck approval, inspect resolution, queue re-enqueue, and worker owner.
5. After a fix, run targeted contracts and then `make test`. Cross-process, security-configuration, or durable-recovery changes require real `make smoke-service` evidence.

A security-policy change is a behavior change: update the Product Spec/API Contract or applicable OpenSpec change before implementation and review.
