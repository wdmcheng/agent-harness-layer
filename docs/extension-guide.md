# Extension guide

[English](extension-guide.md) | [简体中文](extension-guide.zh-CN.md)

Audience: application developers adding business capabilities to `templates/service-app`, and scaffold maintainers evolving public seams.

Navigation: [root README](../README.md) · [Build an Agent](building-an-agent.md) · [architecture boundaries](architecture/README.md) · [adapter contracts](adapter-contracts.md) · [context/trust boundaries](context-and-trust-boundary.md) · [security policy](security-policy.md) · [Eval/Observability](eval-observability-loop.md)

## Extension principles

Choose an existing public seam before adding an implementation. `agents/*` never imports vendor SDKs directly. `app/*` owns protocol entry points, dependency composition, and response conversion only. Core `agent_harness/*` does not depend on the template or a concrete Agent. External SDKs belong only in `agent_harness/adapters/*` or an integration module explicitly approved by the import boundary. Cross boundaries with Pydantic DTOs, `CanonicalEvent`, protocols, facades, repositories, or UoW—not ORM sessions, SDK objects, or mutable in-process globals.

`make quality` runs `scripts/import_boundary_check.py`. Bypassing the check, moving an SDK into a business Agent, or weakening the rule to make a check green is not an extension pattern.

The direct local CLI commands below assume that you completed [service-app first use](../templates/service-app/README.md#first-use-local-profile), exported `AGENT_HARNESS_BUDGET__FINGERPRINT_KEY` and the storage DSN, and migrated SQLite. Missing prerequisites must fail closed; do not misdiagnose `config.invalid` or `storage.migration_required` as a tool failure.

## Agent

- Public seam: `AgentDescriptor`, `AgentRegistry`, `AgentExecutor`, and each Agent's `config.yaml`, `agent.py`, and `schemas.py`.
- Action: from a copied service-app root, run `uv run agent-harness scaffold agent support.triage`; complete executor/schema; review the generated draft eval case; confirm registry discovery.
- Forbidden: business Agent logic in `app/*`; reading profile YAML from an Agent; delegation outside registry/policy; default tool authority.
- Validate: `uv run agent-harness agents list --profile local --profiles-dir ./configs/profiles --agents-dir ./agents`, then run that Agent and its approved eval.
- Evidence: `tests/contracts/test_agent_scaffold_cli_contracts.py`, `tests/contracts/test_agent_registry_router_model_contracts.py`, `templates/service-app/docs/examples.md`.
- Troubleshoot: if the Agent is missing, inspect the dotted lowercase `agent_id`, configuration schema, and executor import. If scaffold rejects the target, inspect symlinks, existing directories, and root discovery. There is no `--force` option.

## Tools and MCP

- Public seam: `ToolRegistry`, tool descriptor/result DTOs, `WorkspacePolicy`, and `PolicyEngine`; MCP is adapted through `MCPClient` and `MCPTool`. `ApprovedToolExecutor` is an internal registry approval executor, not a public `agent_harness.tools` export.
- Action: register the smallest schema/permission set; configure workspace roots, path rules, and environment allowlists explicitly; dangerous calls must pass policy allow/deny/require-approval.
- Forbidden: direct execution of an unregistered callable; forwarding the whole host environment to shell; path escape; side effects before approval; raw MCP responses in public events.
- Validate: after local initialization, run `uv run agent-harness policy check --profile local --profiles-dir ./configs/profiles --storage-dsn "$STORAGE_DSN" --action run.read --resource run`, then the relevant Agent run and `make test`.
- Evidence: `tests/contracts/test_tool_registry_public_seam_contracts.py`, `tests/contracts/test_tool_registry_authorization_contracts.py`, `tests/contracts/test_approval_execution_contracts.py`.
- Troubleshoot: if a tool is invisible, inspect the Agent allowlist and identity permission. A 409 may require approval. For path denial, inspect the normalized workspace-relative path rather than widening the root.

## Model and embedding

- Public seam: `ModelProvider`, `ModelStructuredProvider`, `ModelRequest`, `ModelResponse`, `StructuredOutputResult`, `ModelRouter`, and `ModelInvocationService`; embeddings use `EmbeddingProvider` and a cache protocol.
- Action: implement providers in `agent_harness/adapters/models/`, bind them in the composition root, keep fake providers limited to explicit local/test routes, and cover usage/cost/latency and replay contracts. For structured results, bind a strict `output_schema` in the Agent `config.yaml`, then call `complete_structured(..., operation_key=..., repair_limit=0..2)` from a bound execution. Callers may narrow the repair limit but cannot submit a different schema.
- Forbidden: vendor SDK imports in business Agents; provider/Pydantic objects in DTOs; skipping identity, budget, policy, or approval before invocation; inventing zero cost when cost is unavailable; adapter-owned repair/retry; raw invalid candidates in evidence; text/fake fallback; or interpreting structured JSON as a tool call.
- Validate: `make test`, `make smoke-local`; run `make smoke-service` only for real cross-process persistence.
- Evidence: `tests/contracts/test_agent_registry_router_model_contracts.py`, `tests/contracts/test_model_usage_invocation_contracts.py`, `tests/contracts/test_model_usage_runtime_composition_contracts.py`, `tests/contracts/test_provider_neutral_structured_public_seam_contracts.py`.
- Troubleshoot: inspect profile, model policy, deployment capability, and Agent `output_schema_identity` for routing failures. For duplicate usage, inspect the stable call ID, operation/replay identity, outbox, and settlement. Fix Registry loading for `model.structured_schema_unknown`; keep reservations fenced and review `needs_review` instead of resending. Retain only redacted stable errors, never raw responses or invalid candidates.

## Retrieval

- Public seam: `RetrievalProvider`, `RetrievalResult`, `ContextFragment`; current implementations include local SQLite BM25, PostgreSQL native FTS, and optional PGroonga/pgvector adapters.
- Action: implement the provider protocol, convert results with `retrieval_result_to_context_fragment` so `source_ref`/`trust_level` survive, and let `ContextAssembler` own budget/trace.
- Forbidden: prompt concatenation that loses source/trust; making an optional extension a local hard dependency; cross-tenant index reads.
- Validate: after local initialization, run `uv run agent-harness doctor --profile local --profiles-dir ./configs/profiles --storage-dsn "$STORAGE_DSN"`, the RAG example, and `make test`; use `make smoke-service` for PostgreSQL.
- Evidence: `tests/contracts/test_retrieval_rag_contracts.py`, `tests/contracts/test_retrieval_doctor_example_contracts.py`.
- Troubleshoot: if an extension is unavailable, verify capability probing and native-FTS fallback. For empty results, inspect tenant, metadata, and index rather than hiding failure with unsourced text.

## Observability

- Public seam: `TelemetryFacade`, `ProviderTelemetryAdapter`, `TelemetryRecord`, `CanonicalEvent`, and OTel mapping.
- Action: commit local evidence before fan-out to optional Logfire/Phoenix/Langfuse adapters. A new provider implements the protocol, redaction, and degradation state without changing business callers.
- Forbidden: provider SDK imports in business code; rolling back a local event because a provider failed; sending secrets, absolute paths, or oversized raw payloads.
- Validate: `make smoke-local`, `make test`; provider configuration/degradation contracts are listed below.
- Evidence: `tests/contracts/test_observability_local_first_fanout_contracts.py`, `tests/contracts/test_observability_provider_adapters_contracts.py`, `tests/contracts/test_observability_provider_configuration_contracts.py`.
- Troubleshoot: when a provider is degraded, verify local `CanonicalEvent`/JSONL first, then inspect the redacted status summary. Do not report an unavailable SaaS as a failed run.

## Eval

- Public seam: approved-case repository, `ApprovedCaseExecutor`, `ExperimentEvaluator`, `ExperimentEvidencePublisher`, score sink, and acceptance service.
- Action: detectors write drafts only; human review promotes to approved. Experiments freeze split/manifest, compare baseline/candidate, and leave acceptance to reviewer, policy, and audit.
- Forbidden: automated writes to approved; aggregate improvements overriding holdout/critical regressions; evaluators directly editing production prompt/config; provider failure presented as successful local evidence.
- Validate: `make eval`, `make test`; see the [Eval/Observability loop](eval-observability-loop.md) for HTTP/CLI experiment operations.
- Evidence: `tests/contracts/test_eval_gate_trace_loop_contracts.py`, `tests/contracts/test_eval_execution_contracts.py`, `tests/contracts/test_eval_experiment_api_contracts.py`.
- Troubleshoot: `no-approved-cases` is a stable result, not a fabricated pass. Human review must inspect claim/evaluator evidence for `needs_review`; do not force reruns.

## Before completion

```bash
make quality
make test
make eval
make smoke-local
# Use this to prove service behavior only when the extension affects PostgreSQL, Redis,
# DBOS, or API/worker collaboration:
make smoke-service
```

Behavior changes first update the Product Spec/DEV Plan or the applicable OpenSpec change. API changes update `API-Contract.md` first. A new vendor or runtime also requires review of [adapter contracts](adapter-contracts.md), [ADR-0002](adr/0002-vendor-adapter-isolation.md), and the [release process](release-process.md).
