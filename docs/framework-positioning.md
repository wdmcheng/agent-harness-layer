# Framework positioning and capability comparison guide

[English](framework-positioning.md) | [简体中文](framework-positioning.zh-CN.md)

Navigation: [root README](../README.md) · [Build an Agent](building-an-agent.md) · [architecture](architecture/README.md) · [extension guide](extension-guide.md)

This document answers a question that the five-layer guide intentionally does not: how does Agent Harness Layer relate to Pydantic AI, `pydantic-ai-harness`, and Agently, and what can we learn from their capability designs?

The short answer is that this repository is an enterprise control plane, not a thin wrapper around either reference framework. Pydantic AI is the model-facing foundation. `pydantic-ai-harness` is a capability library that composes with Pydantic AI. Agently is a broader AI application runtime with its own request, Action, Skill, task, and workflow owners. This project owns identity, policy, HITL, durable runs, checkpoints, budgets, tenant boundaries, local-first evidence, eval acceptance, and release gates.

## How to read this comparison

This document explains capability boundaries and design differences. It is not a copy-paste tutorial for another runtime. In this repository:

- business Agents use `agent_harness` public DTOs, protocols, registries, facades, repositories, and UoW;
- vendor SDKs and other framework runtime objects stay behind adapter or integration boundaries;
- tools go through `ToolRegistry` and `PolicyEngine`; dangerous actions may wait for HITL;
- sub-agents go through `AgentRegistry`, delegation edges, and shared-parent budget accounting;
- local `CanonicalEvent`, usage, audit, checkpoint, and eval evidence remain the source of truth;
- a capability being available in another framework does not mean it is enabled, supported, or verified here.

## Quick Start for this repository

Use the service-app template for the first runnable Agent. This is the supported path:

```bash
cd templates/service-app
make bootstrap

export AGENT_HARNESS_BUDGET__FINGERPRINT_KEY="$([[ -n \"$AGENT_HARNESS_BUDGET__FINGERPRINT_KEY\" ]] && printf %s \"$AGENT_HARNESS_BUDGET__FINGERPRINT_KEY\" || uv run python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export STATE_DIR="$PWD/.agent-harness/local"
export STORAGE_DSN="sqlite+aiosqlite:///$STATE_DIR/agent_harness.db"
mkdir -p "$STATE_DIR"

uv run python app/migrate.py \
  --profile local \
  --profiles-dir ./configs/profiles \
  --storage-dsn "$STORAGE_DSN"

uv run agent-harness scaffold agent support.triage
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

The initialization, API, eval, observability, and handoff details remain in the [service-app README](../templates/service-app/README.md), [example Agents](../templates/service-app/docs/examples.md), and [AI / Agent project guide](../templates/service-app/docs/ai-agent-guide.md). Do not replace this path with another framework's global settings or execution loop.

## Capability matrix: current, reference, and not adopted

| Capability | Agent Harness Layer today | Pydantic AI Harness reference | Agently reference | Local decision |
|---|---|---|---|---|
| Model request and structured output | Thin provider adapter; public response is not a full other-framework runtime object | Pydantic AI capabilities and hooks compose at `Agent` level | `.output(...)`, parsing, validation, retries, and result views | Keep business code vendor-neutral; any syntax sugar must use existing model seams |
| Tool execution | `ToolRegistry` → policy/workspace → optional HITL → audit/artifact | `CodeMode`, MCP, search, filesystem and shell capabilities | Action Runtime and ExecutionResource | Do not bypass registry or approval |
| Sub-agents | Declared delegation edges, checkpoints, shared-parent budget | `DynamicWorkflow` and sub-agent capability patterns | AgentTask, TaskDAG, TriggerFlow, team patterns | Keep delegation explicit and durable |
| Workflow | `RunOrchestrator`, queue, checkpoint, resume, service worker | DynamicWorkflow is model-authored and owned by the reference runtime | TriggerFlow and Dynamic Task | Do not add a second scheduler implicitly |
| Skills | `.agents/skills` is development-time guidance; runtime Skill catalog is not a current public seam | Skills are tracked as a reference capability area | SkillLibrary + exact revisions + TaskContext disclosure | If added, use trusted revisions and existing policy/audit |
| Memory/session | Context assembly, repository/UoW, checkpoint, tenant/run correlation | Memory, sliding window, compaction, persistence capabilities | Session, TaskContext, records, snapshots | No hidden model calls or second persistence truth |
| Eval | Approved-only cases, experiments, human acceptance, `needs_review` | Capability matrix lists verification/eval-related building blocks | Evaluator/reviser and task evidence patterns | Preserve human approval as the gate |
| Observability | Local `CanonicalEvent`, usage, audit first; optional provider fan-out | Logfire and capability traces | Observation events, DevTools, execution records | Provider degradation cannot erase local evidence |
| Release and governance | License, artifact, CI evidence, local/service/hosted boundaries | Reference package release policy | Runtime framework release/version policy | Keep release truth in this repository |

The matrix is a boundary map, not a promise that every comparison row will be implemented. It prevents a common mistake: treating a capability matrix as an installation checklist and silently introducing a second runtime.

## Why use this project instead of another framework runtime?

Choose this project when the application needs stable ownership for identity, tenant isolation, permissions, approvals, durable recovery, budgets, audit, local-first evidence, eval acceptance, or service deployment. Choose direct Pydantic AI when one or two prompts and provider-native features are the whole product. Study `pydantic-ai-harness` when a narrowly scoped capability can be adapted without taking over runtime ownership. Study Agently when you need a reference for structured outputs, Action/Skill lifecycle, task evidence, or signal-driven workflows.

The differences are about center of gravity:

| Center of gravity | Strength | Why it is not the current default here |
|---|---|---|
| Direct SDK/Pydantic AI | Small surface and provider-native features | Leaves enterprise runtime, governance, and evidence ownership to the application |
| Pydantic AI Harness | Composable capability bundles around Pydantic AI | Current repository pins an older Pydantic AI baseline and has different policy/tool/runtime owners |
| Agently | Integrated request, Action, Skill, task, and workflow runtime | Would introduce overlapping execution, persistence, and lifecycle contracts |
| Agent Harness Layer | Governed service boundary around business Agents | Intentionally narrower; capabilities from other frameworks must cross explicit adapters |

## Reading and adoption order

Read the reference documentation for semantics, then return to the matching local contract:

1. Start with this repository's [five-layer guide](building-an-agent.md) and template Quick Start.
2. Read Pydantic AI Harness [README](https://github.com/pydantic/pydantic-ai-harness/blob/main/README.md), especially Quick start, DynamicWorkflow, and Capability matrix.
3. Read Agently [Why Agently](https://github.com/AgentEra/Agently#why-agently), [Framework Positioning](https://github.com/AgentEra/Agently#framework-positioning), and [Quickstart](https://github.com/AgentEra/Agently#quickstart).
4. Map the desired behavior to [extension guide](extension-guide.md), [adapter contracts](adapter-contracts.md), [context/trust boundary](context-and-trust-boundary.md), and [security policy](security-policy.md).
5. If the capability changes a public contract, persistence owner, budget meaning, or security boundary, stop treating it as documentation and design a separate change before implementation.

Reference pages change independently. Their examples are linked for orientation; this repository's pinned dependencies, public exports, tests, and release evidence remain authoritative for what can run here.
