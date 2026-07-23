# Build an Agent with five layers and two wings

[English](building-an-agent.md) | [简体中文](building-an-agent.zh-CN.md)

Audience: developers creating their first business Agent with Agent Harness Layer, and maintainers deciding where a feature belongs.

Navigation: [root README](../README.md) · [service-app template](../templates/service-app/README.md) · [architecture](architecture/README.md) · [extension guide](extension-guide.md) · [example Agents](../templates/service-app/docs/examples.md)

This guide answers one practical question: how do you turn a business idea into a runnable, evaluable, and observable Agent using the five-layer, two-wing architecture?

You do not rebuild seven systems for every Agent. The template already supplies most of Access and Runtime. A minimal Agent mainly implements typed schemas and an executor in Engine, declares model and budget choices in configuration, adds Tools and Infra only when required, and uses Eval plus Observability throughout the lifecycle.

## What to change in each area

| Architecture area | What an Agent developer does | Primary files or public seams | Required for a minimal Agent? |
|---|---|---|---|
| 1. Access and interaction | Choose CLI or HTTP and reuse authentication, request validation, OpenAPI, SSE, and error envelopes | `templates/service-app/app/api/`, core `agent-harness` CLI | No; normally reuse it |
| 2. Orchestration and Runtime | Make the Agent discoverable and let runtime own runs, idempotency, checkpoints, resume, approvals, and delegation | `config.yaml`, `AgentRegistry`, `RunOrchestrator`, `AgentExecutionResult` | Implement the executor/config contract; do not build another scheduler |
| 3. Engine and cognition | Define typed input/output, business reasoning, model choice, budget, and optional delegation | `schemas.py`, `agent.py`, `config.yaml`, model/context public seams | Yes; this is the business Agent |
| 4. Tools and capabilities | Define the smallest required tool schemas, allowlist, workspace/policy, and HITL boundaries | `tools.py`, `tool_allowlist`, `ToolRegistry`, `WorkspacePolicy`, `PolicyEngine` | Only when the Agent needs an external action |
| 5. Infrastructure and data | Select local/service profile and configure storage, queue, model, retrieval, secrets, and business adapters | `configs/profiles/`, `app/runtime.py`, repository/UoW, provider adapters | Local needs no development; configure or extend for real dependencies |
| Left wing: Eval Gate | Capture reproducible behavior as draft, obtain human approval, then use approved cases as regression/release evidence | `evals/drafts/`, `evals/approved/`, `EvalRunner`, experiment/acceptance seams | Establish a minimum regression; automation cannot approve a case |
| Right wing: Observability | Read run/event/usage/audit evidence locally first, then optionally fan out to providers | `CanonicalEvent`, events CLI/SSE, local JSONL/PostgreSQL sinks, `TelemetryFacade` | Preserve local evidence; external providers are optional |

`Graph Nodes`, `GraphState`, complex long-term memory, and independent tool/model gateways in the architecture are future extension points, not prerequisites for the first Agent. The diagram's `@agent.tool Registry` label is conceptual: the current public seam is `ToolRegistry` with typed descriptor/result DTOs. There is no public decorator that bypasses registry, policy, or approval.

## How one request uses all seven areas

```text
CLI / HTTP
  -> Access: authentication, tenant, input schema, request/trace ID
  -> Runtime: registry lookup, idempotent run, checkpoint, budget, approval/delegation
  -> Engine: executor consumes typed input and returns completed/waiting/failed
  <-> Tools: model or executor can call only registered and authorized capabilities
  -> Infra: storage, queue, model, retrieval, business systems

Eval Gate: execute approved cases against the same Agent behavior and block regressions
Observability: record CanonicalEvent, usage, audit, and security evidence at every stage
```

The wings are not two plugins at the end of the request. Eval constrains output and safety design from the start. Observability establishes tenant/Agent/run/request/trace correlation when the request enters and follows runtime, tools, models, storage, and the result. Every run record and its evidence must retain `tenant_id`, `agent_id`, and `run_id`; request/trace IDs add correlation rather than replace those identities.

## Create a minimal Agent from scratch

### 1. Initialize the template

Follow [service-app first use](../templates/service-app/README.md#first-use-local-profile) to bootstrap the package, create a fingerprint key, migrate SQLite, and verify the local profile. `create_app`, CLI run, and worker intentionally fail closed before migration.

If an AI / Agent is doing the work, send it the copied template and ask it to read the ordinary [AI / Agent project guide](../templates/service-app/docs/ai-agent-guide.md). The guide contains initialization, implementation, validation, authority, and handoff instructions without automatically imposing directory-level rules.

### 2. Generate a safe skeleton

From the service-app root:

```bash
uv run agent-harness scaffold agent support.triage
```

The result is `agents/support/triage/` with `agent.py`, `schemas.py`, `tools.py`, `config.yaml`, and draft/approved eval directories. The command does not overwrite an existing directory and does not grant tool or delegation permissions.

### 3. Implement Engine

Define input/output DTOs in `schemas.py`. Then make the module-level `executor` in `agent.py` satisfy the `AgentExecutor` protocol. Each execution returns exactly one valid result:

- `AgentExecutionResult.completed(output)` for a final result;
- `AgentExecutionResult.waiting(approval)` when HITL must decide;
- `AgentExecutionResult.failed(error)` for structured failure.

See the template's [Create an Agent](../templates/service-app/README.md#create-an-agent) section for a complete executor and `config.yaml`. Keep business logic out of `app/api`; do not read profile YAML or ORM sessions directly from the executor.

### 4. Connect it to Runtime through configuration

At minimum, `config.yaml` declares:

- a stable `agent_id`, version, name, and description;
- input/output schema import paths;
- `executor: agent:executor`;
- model provider/default model;
- token/cost budgets;
- empty-by-default `tool_allowlist` and `delegation_edges`;
- the approved eval dataset path.

This declarative registration is a real ergonomic layer: you do not wire every Agent into FastAPI routes or runtime composition. `AgentRegistry.load_from_directory()` validates configuration, schemas, and executor together. It does not bypass runtime, policy, or storage.

### 5. Verify with CLI before adding HTTP integration

```bash
uv run agent-harness agents list \
  --profile local \
  --profiles-dir ./configs/profiles \
  --agents-dir ./agents \
  --storage-dsn "$STORAGE_DSN"

uv run agent-harness run support.triage \
  --profile local \
  --profiles-dir ./configs/profiles \
  --agents-dir ./agents \
  --storage-dsn "$STORAGE_DSN" \
  --prompt 'login stopped working'
```

In a copied project, `run` and `agents list` need the explicit `--agents-dir ./agents`; project-root discovery belongs only to `scaffold agent`. CLI and HTTP still enter the same registry, runtime, DTO, storage, and event seams. Validate the business Agent through CLI first, then use the template's [HTTP API](../templates/service-app/README.md#http-api) when application integration is required.

## When to extend Tools and Infra

| Requirement | Add | Do not |
|---|---|---|
| Typed classification, extraction, or deterministic processing | Keep `tool_allowlist` empty; implement schema/executor only | Pre-authorize file, shell, or network access for possible future use |
| Knowledge retrieval | Implement `RetrievalProvider`; return `ContextFragment` with `source_ref`/`trust_level` | Insert unsourced retrieval text into a trusted prompt |
| Workspace read/write | Register a minimal file tool through `ToolRegistry`; configure `WorkspacePolicy`/`.agentignore` | Call the filesystem directly and bypass path/artifact boundaries |
| Dangerous action | Add policy and HITL, return `waiting`, decide through approvals CLI/API | Treat a public resume token as approval or create side effects before approval |
| Real model | Implement a provider at the controlled adapter/integration boundary; record usage/cost/latency | Import a vendor SDK in a business Agent or expose raw provider objects |
| Multi-Agent collaboration | Declare `delegation_edges`; let registry, policy, and shared parent budget govern it | Recursively call another Agent from an executor |
| Service profile | Configure PostgreSQL, Redis, secrets, migration, API/worker, and run service smoke | Claim cross-process recovery from local SQLite evidence |

Add Tools and Infra in response to a requirement. The existence of `tools.py` does not mean a tool must be registered, and configurable providers do not authorize business code to depend directly on their SDKs.

## Connect the two wings

### Eval Gate: create reviewable evidence

1. Record a validated input, observed output, and human-confirmed expectation as a draft.
2. Have a reviewer inspect behavior, safety, and privacy before approval.
3. Let `EvalRunner` execute approved cases only.
4. Compare candidate behavior through optimization/holdout/regression, while reviewer, policy, and audit still decide acceptance.

Start with:

```bash
make eval
```

Draft/approve/run commands are in the [example Agent guide](../templates/service-app/docs/examples.md). Experiment and acceptance boundaries are in the [Eval and Observability loop](eval-observability-loop.md). If no approved case exists, the stable outcome is `no-approved-cases`, not “evaluation passed.”

### Observability: understand what the Agent did

Keep the real `run_id`, then read its events:

```bash
uv run agent-harness events stream "$RUN_ID" \
  --profile local \
  --profiles-dir ./configs/profiles \
  --storage-dsn "$STORAGE_DSN" \
  --events-path "$STATE_DIR/traces.jsonl"
```

Confirm local `CanonicalEvent`, usage, and audit evidence before adding Logfire, Phoenix, or Langfuse. An external provider failure may create a degradation state; it must not roll back local evidence or falsify the run outcome.

## Choose the implementation scope by complexity

| Goal | Five-layer scope | Minimum two-wing evidence |
|---|---|---|
| First runnable Agent | Reuse Access/Runtime; implement Engine; no Tools; local/fake Infra | One human-approved case and readable local events |
| Tool-enabled Agent | Add `ToolRegistry`, allowlist, policy/workspace, and risk-based HITL | Eval allow/deny/waiting; tool/audit events |
| RAG Agent | Add retrieval/context assembly and trusted source metadata | Eval no-hit, low-quality, and injected content; observe retrieval/model usage |
| Multi-Agent workflow | Add delegation controlled by registry/policy/budget | Eval parent/child budget and failure propagation; preserve correlation |
| Production candidate | Switch to service profile and verify migration, auth, API/worker/queue recovery | Approved regression gate; local evidence first, provider fan-out degradable |

## Map `support.triage` to the architecture

For “read ticket text and return category, confidence, and whether human review is needed”:

1. **Access:** use CLI first; reuse `POST /api/v1/agents/{agent_id}/runs` when a business system needs HTTP.
2. **Runtime:** register `support.triage` in `config.yaml`; registry/runtime owns the run. Low confidence may return `needs_review`; use approval only for dangerous actions.
3. **Engine:** define ticket input/classification output in `schemas.py`; implement the classifier executor in `agent.py`.
4. **Tools:** leave the allowlist empty for text-only classification; add a controlled tool only when CRM lookup is required.
5. **Infra:** start with local profile/fake model; add an adapter and secret configuration when a real model or CRM is required.
6. **Eval Gate:** draft normal, ambiguous, injected, and missing-field inputs; run the dataset only after human approval.
7. **Observability:** inspect run/event/usage; record explainable structured output for ambiguous input without retaining raw secrets.

That is how the architecture is used: identify what the framework owns, what the business Agent implements, which capabilities remain disabled until required, and how the wings prove that behavior is acceptable.

## Completion checklist

- Registry lists the Agent and validates its schema, executor, and configuration.
- CLI completes at least one real run; verify HTTP only when it is needed.
- Tools, retrieval, and delegation use least privilege; unused capabilities stay disabled.
- Local/service evidence boundaries are honest; introduce migration, queue, and worker only when needed.
- At least one eval is human-approved, with cases for failure and safety paths.
- A `run_id` locates events, usage, audit, and required artifacts without leaking secrets or raw provider payloads.
- Extensions depend only on public DTO/protocol/registry/facade/repository seams, never internal implementations or vendor objects.

See the [extension guide](extension-guide.md) for individual extension patterns, [`API-Contract.md`](../API-Contract.md) for field-level HTTP contracts, and [the four example Agents](../templates/service-app/docs/examples.md) for runnable implementations.
