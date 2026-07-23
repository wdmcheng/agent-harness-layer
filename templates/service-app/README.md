# Agent Harness Service App Template

[English](README.md) | [简体中文](README.zh-CN.md)

This directory is a copyable backend application template built on the `agent-harness` core package. It assembles FastAPI, a thin service CLI, a runtime worker, typed local/service profiles, Docker Compose, example agents, eval data, and public-seam tests without moving business agent logic into the application entry layer.

Use this template when you want to build an agent service. Work in the repository root only when you intend to change the reusable core package or the template itself.

## Ask an AI / Agent to work on the project

This template includes an ordinary, opt-in [AI / Agent project guide](docs/ai-agent-guide.md). It is not an automatic instruction file. Send the link or paste this prompt when you want an AI to initialize the copied project or implement a feature:

```text
Read docs/ai-agent-guide.md first, then inspect this project and complete this task:
<task and acceptance criteria>

Follow the guide's architecture, security, validation, and handoff rules. Do not commit,
push, deploy, publish, use production credentials, or call a real provider unless I
separately authorize that exact action.
```

The guide contains fuller copyable prompts for project initialization and feature implementation.

## What you get

- `local` profile: SQLite, in-memory queue, local JSONL evidence, fake model, no external provider key;
- `service` profile: PostgreSQL, Redis, migration, FastAPI API, and a separate runtime worker;
- HTTP management surface under `/api/v1`, plus OpenAPI, Swagger, and Redoc;
- the core `agent-harness` CLI for agents, runs, events, tools, policy, approvals, eval, and scaffolding;
- an app-specific `agent-harness-service serve` command;
- four runnable examples: RAG assistant, ticket triage, repository analyst, and development assistant;
- an `examples.basic` deterministic smoke fixture;
- an atomic agent generator with safe defaults and a draft eval case;
- copied-project quality, test, eval, local smoke, and real service smoke commands.

The API and worker are separate processes in the service profile. Model/tool gateways, an event pipeline, and a storage service are future boundaries, not current Compose services.

## Prepare the environment

Required for local use:

- macOS or Linux;
- Python `>=3.12`;
- Git and GNU Make;
- uv `>=0.11.29,<0.12` in the source repository and release wrappers; CI currently selects `0.11.29`, while release artifacts record the actual patch;
- a trusted local `agent-harness` wheel, sdist, source directory, or private index when using a copied template.

Check the toolchain:

```bash
python3 --version
uv --version
git --version
make --version
```

`make smoke-service` additionally requires Docker with Compose v2. A real model API key is not required by either default profile because both use the fake model unless you deliberately configure another provider.

## First use: local profile

### 1. Select the core package source

Inside the source repository workspace:

```bash
cd templates/service-app
make bootstrap
```

After copying this directory into an independent project, provide a trusted artifact or source path on the first bootstrap:

```bash
make bootstrap \
  AGENT_HARNESS_SOURCE=/absolute/path/to/agent_harness-0.1.0-py3-none-any.whl
```

`bootstrap` records the trusted local source in the copied project's `tool.uv.sources`, so later commands can reuse it. If your organization publishes `agent-harness==0.1.0` to a trusted private index, configure `UV_INDEX_URL` and opt in explicitly:

```bash
make bootstrap AGENT_HARNESS_ALLOW_INDEX=1
```

An independent template does not resolve a public package with the same name by default. That is a supply-chain boundary, not an installation defect.

### 2. Create local state and migrate it

Generate a fingerprint key for this state database, export the database location, and run migration:

```bash
export AGENT_HARNESS_BUDGET__FINGERPRINT_KEY="$(
  uv run python -c 'import secrets; print(secrets.token_urlsafe(32))'
)"
export STATE_DIR="$PWD/.agent-harness/local"
export STORAGE_DSN="sqlite+aiosqlite:///$STATE_DIR/agent_harness.db"
export AGENT_HARNESS_STORAGE__DSN="$STORAGE_DSN"

mkdir -p "$STATE_DIR"
uv run python app/migrate.py \
  --profile local \
  --profiles-dir ./configs/profiles \
  --storage-dsn "$STORAGE_DSN"
```

The fingerprint key is a budget request identity secret, not a model API key. Keep it stable for the lifetime of this state database. Do not write the value into `local.yaml`, documentation, or Git.

You may copy `.env.example` to the ignored `.env` file for persistent local overrides, but it must contain an environment-specific key rather than a repository default:

```bash
cp .env.example .env
```

### 3. Verify the profile and run the first agent

```bash
make smoke-local
make run-basic
```

`make smoke-local` validates configuration and registry discovery. `make run-basic` executes the actual registry/runtime/event path and prints a `run_id`, status, and terminal event.

### 4. Start the API

```bash
make dev
```

The equivalent explicit command is:

```bash
uv run agent-harness-service serve \
  --profile local \
  --profiles-dir ./configs/profiles \
  --storage-dsn "$STORAGE_DSN" \
  --host 127.0.0.1 \
  --port 8000
```

Open:

- health: `http://127.0.0.1:8000/api/v1/health`
- Swagger UI: `http://127.0.0.1:8000/docs`
- Redoc: `http://127.0.0.1:8000/redoc`
- OpenAPI JSON: `http://127.0.0.1:8000/openapi.json`

## Everyday usage

### Make commands

| Command | Purpose |
|---|---|
| `make bootstrap` | resolve the trusted core package source and sync dependencies |
| `make dev` | start FastAPI with the selected profile |
| `make cli ARGS='<core command>'` | call the core CLI without duplicating command logic |
| `make run-basic` | execute the deterministic smoke agent |
| `make run-rag` | run the RAG assistant example |
| `make run-ticket` | run the ticket triage example |
| `make run-repo` | run the repository analyst example |
| `make run-dev` | run the development assistant example |
| `make test` / `make contract` | run copied-template public-seam tests |
| `make quality` | run Ruff and Pyright over app, agents, tests, and scripts |
| `make eval` | run all approved example eval cases |
| `make eval-rag` |eval-ticket|eval-repo|eval-dev` | run one example's eval cases |
| `make smoke-local` | validate the local profile and agent registry |
| `make smoke-service` | run the real copied-template PostgreSQL/Redis/API/worker smoke |
| `make worker` | start the runtime worker using the selected profile |

All targets accept Make variable overrides such as `PROFILE`, `PROFILES_DIR`, `STATE_DIR`, `STORAGE_DSN`, `EVENTS_PATH`, `HOST`, and `PORT`.

## CLI

The template CLI owns only `serve`. All management operations come from the core CLI so HTTP, CLI, and worker paths share the same DTOs, services, errors, and authorization semantics.

### Check configuration

```bash
uv run agent-harness doctor \
  --profile local \
  --profiles-dir ./configs/profiles \
  --storage-dsn "$STORAGE_DSN"
```

### List and run agents

```bash
uv run agent-harness agents list \
  --profile local \
  --profiles-dir ./configs/profiles \
  --agents-dir ./agents \
  --storage-dsn "$STORAGE_DSN"

uv run agent-harness run examples.basic \
  --profile local \
  --profiles-dir ./configs/profiles \
  --agents-dir ./agents \
  --storage-dsn "$STORAGE_DSN" \
  --idempotency-key first-cli-run
```

In a copied service-app, always pass `--agents-dir ./agents` to `run` and `agents list`, as the examples above do. Their current default remains the source-workspace path `templates/service-app/agents`; it does not discover the copied project. The `make run-*` targets pass the application agents directory explicitly. Project-root discovery belongs only to `scaffold agent`, which uses the copied project's `pyproject.toml` marker when `--agents-dir` is omitted.

### Stream events

Capture a run ID from real output, then stream canonical NDJSON:

```bash
RUN_OUTPUT="$(make run-basic)"
printf '%s\n' "$RUN_OUTPUT"
export RUN_ID="$(printf '%s\n' "$RUN_OUTPUT" | awk '/^run_id:/ {print $2; exit}')"
test -n "$RUN_ID"

uv run agent-harness events stream "$RUN_ID" \
  --profile local \
  --profiles-dir ./configs/profiles \
  --storage-dsn "$STORAGE_DSN" \
  --events-path "$STATE_DIR/traces.jsonl"
```

CLI `--after-seq` is an exclusive cursor. The HTTP SSE route uses the single `Last-Event-ID` header instead. Both default to public events and stop after the terminal event.

### Check policy

```bash
uv run agent-harness policy check \
  --profile local \
  --profiles-dir ./configs/profiles \
  --storage-dsn "$STORAGE_DSN" \
  --action run.read \
  --resource run
```

Use `agent-harness --help` and `<group> --help` for tools, approvals, eval, experiments, local-state migration, and their exact options.

## HTTP API

Local profile uses a default development identity. Service profile requires `Authorization: Bearer <token>` and derives tenant/user/permissions from the server-side verifier.

### Common routes

| Method and path | Purpose |
|---|---|
| `GET /api/v1/health` | public liveness/configuration capability summary |
| `GET /api/v1/agents` | list visible agent descriptors |
| `POST /api/v1/agents/{agent_id}/runs` | create a run |
| `GET /api/v1/runs/{run_id}` | read durable run detail |
| `POST /api/v1/runs/{run_id}/cancel` | cancel a non-terminal run |
| `POST /api/v1/runs/{run_id}/resume` | resume a normal checkpoint; not an approval bypass |
| `GET /api/v1/runs/{run_id}/events` | read JSON events using `after_seq` |
| `GET /api/v1/runs/{run_id}/events/stream` | read SSE using `Last-Event-ID` |
| `GET /api/v1/runs/{run_id}/approvals` | list run approvals |
| `GET /api/v1/runs/{run_id}/approvals/{approval_id}` | read one approval |
| `POST /api/v1/runs/{run_id}/approvals/{approval_id}` | approve or deny a waiting action |
| `POST /api/v1/policies/check` | evaluate a policy action/resource/context |
| `/api/v1/eval-cases/*` | draft, list, and approve eval cases |
| `/api/v1/evals/runs/*` | run approved evals and read scores |
| `/api/v1/evals/experiments/*` | create, compare, and accept experiments |

There is intentionally no remote `/api/v1/tools` endpoint. Tool execution remains behind the CLI/runtime `ToolRegistry` seam.

### Create and inspect a run

```bash
curl -sS http://127.0.0.1:8000/api/v1/agents

RUN_JSON="$(curl -sS \
  -H 'Content-Type: application/json' \
  -H 'X-Request-Id: readme-first-run' \
  -H 'X-Trace-Id: readme.first.run' \
  -d '{"input": {}, "idempotency_key": "readme-first-run"}' \
  http://127.0.0.1:8000/api/v1/agents/examples.basic/runs)"
printf '%s\n' "$RUN_JSON"

export RUN_ID="$(RUN_JSON="$RUN_JSON" uv run python -c \
  'import json, os; print(json.loads(os.environ["RUN_JSON"])["run_id"])')"

curl -sS "http://127.0.0.1:8000/api/v1/runs/$RUN_ID"
curl -sS "http://127.0.0.1:8000/api/v1/runs/$RUN_ID/events?after_seq=0"
```

### Stream with SSE

```bash
curl -N \
  -H 'Accept: text/event-stream' \
  -H 'Last-Event-ID: 0' \
  "http://127.0.0.1:8000/api/v1/runs/$RUN_ID/events/stream"
```

The cursor is exclusive. A terminal event closes the stream. `include_internal=true` requires additional policy permission.

### Check a policy decision

```bash
curl -sS \
  -H 'Content-Type: application/json' \
  -d '{"action": "run.read", "resource": "run", "context": {}}' \
  http://127.0.0.1:8000/api/v1/policies/check
```

For the service profile, add `-H "Authorization: Bearer $SERVICE_TOKEN"`. Do not put tenant, reviewer, or permission identity into the request body.

Use live Swagger/Redoc for exploration. In the source repository, [`../../API-Contract.md`](../../API-Contract.md) is the field-level source of truth for schemas, status codes, idempotency, approval, event visibility, and recovery rules.

## Create an agent

### Generate the package

From the service-app root:

```bash
uv run agent-harness scaffold agent support.triage
```

This creates `agents/support/triage/` with:

```text
support/triage/
├── __init__.py
├── agent.py
├── tools.py
├── schemas.py
├── config.yaml
└── evals/
    ├── drafts/example.yaml
    └── approved/
```

The generated package uses a fake model, an empty tool allowlist, empty delegation edges, a typed executor, and a draft-only eval. There is no `--force`; existing paths, invalid IDs, path traversal, and symlink escape are rejected before publication.

### Implement the executor

An Agent exposes a module-level executor that satisfies the public runtime protocol:

```python
from agent_harness.runtime import (
    AgentExecutionContext,
    AgentExecutionRequest,
    AgentExecutionResult,
    ApprovalGrant,
)


class SupportTriageExecutor:
    async def run(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
    ) -> AgentExecutionResult:
        del context
        return AgentExecutionResult.completed(
            {"category": "unknown", "input": request.input, "needs_review": True}
        )

    async def resume(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
        grant: ApprovalGrant,
    ) -> AgentExecutionResult:
        del request, context, grant
        return AgentExecutionResult.failed("no approval continuation is defined")


executor = SupportTriageExecutor()
```

Use `schemas.py` for validated input/output DTOs and `config.yaml` for registration:

```yaml
agent_id: support.triage
version: 0.1.0
name: Support Triage Agent
description: Classifies support requests for human routing.
input_schema: agents.support.triage.schemas.SupportInput
output_schema: agents.support.triage.schemas.SupportOutput
executor: agent:executor
model:
  provider: fake
  default_model: fake-scaffold
  fallback_models: []
budget:
  max_tokens_per_run: 1024
  max_cost_usd_per_run: null
tool_allowlist: []
eval_dataset: agents/support/triage/evals/approved
delegation_edges: []
```

Then validate discovery and execution:

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

Review the generated draft before moving it through the approved eval flow. Scaffold generation never auto-approves a case.

## Map your Agent to five layers and two wings

The template means you do not build every architecture area yourself:

| Area | What to do in this copied application |
|---|---|
| Access | normally reuse `app/api`, the core CLI, authentication, OpenAPI, and SSE; add a route only for a real application protocol need |
| Runtime | declare the Agent in `config.yaml` and return `AgentExecutionResult`; let registry/runtime own runs, checkpoints, approvals, and delegation |
| Engine | implement typed schemas and the executor in the Agent package; configure model and budget rather than importing a vendor SDK |
| Tools | keep the allowlist empty unless needed; then register minimal typed tools with workspace, policy, and HITL boundaries |
| Infra | start with the local profile; add retrieval, providers, PostgreSQL/Redis, or business adapters only when required |
| Eval Gate | review generated drafts before `approved`; run approved cases as regression evidence |
| Observability | read run events, usage, and audit locally first; provider telemetry remains optional fan-out |

The normal flow is `CLI/HTTP -> Access -> Runtime -> Engine <-> Tools -> Infra`; Eval tests the same behavior, while Observability records each stage. Graph Nodes/GraphState and independent gateways shown in the product architecture are future extension points, not prerequisites. The diagram's conceptual `@agent.tool` label means the public `ToolRegistry`, not a decorator shortcut.

The source repository provides the complete [five-layer, two-wing Agent development guide](../../docs/building-an-agent.md). That repository-level link will not travel with a standalone copy, so this table deliberately keeps the essential mapping here. The local [AI / Agent project guide](docs/ai-agent-guide.md) does travel with the copy and tells an AI how to apply these boundaries.

## Python composition API

Application code normally uses the CLI and HTTP surface. When embedding the package, import explicit public modules:

```python
from pathlib import Path

from agent_harness.config import load_settings
from agent_harness.registry import AgentRegistry

settings = load_settings(
    profile="local",
    profiles_dir=Path("configs/profiles"),
)
registry = AgentRegistry.load_from_directory(Path("agents"))

print(settings.profile)
print([item.agent_id for item in registry.list_agents()])
```

The template application factory is also injectable for route and health tests. Passing both `orchestrator` and `event_sink` avoids composing storage, but any endpoint exercised by the test still needs a real implementation or a purpose-built test double:

```python
from pathlib import Path
from typing import Any, cast

from agent_harness.events import LocalJsonlEventSink
from agent_harness.registry import AgentRegistry
from app.main import create_app

app = create_app(
    orchestrator=cast(Any, object()),
    event_sink=LocalJsonlEventSink(Path(".agent-harness/test-events.jsonl")),
    registry=AgentRegistry.load_from_directory(Path("agents")),
    approval_service=cast(Any, object()),
    eval_service=cast(Any, object()),
    profile="local",
    profiles_dir=Path("configs/profiles"),
)

assert "/api/v1/health" in app.openapi()["paths"]
```

This minimal injection is only for route-shape or health tests; use a real `RunOrchestrator` and service dependencies when exercising those endpoints. Production startup should still use `agent-harness-service serve` or an equivalent controlled process entrypoint so migration and configuration errors fail before listening.

## Ergonomic layers and “syntax sugar”

The template has deliberate convenience features:

- **Make targets** shorten trusted, repeatable CLI/script invocations.
- **Scaffold project-root discovery** lets `scaffold agent` find `./agents` from the copied project's marker; `run` and `agents list` still require an explicit `--agents-dir ./agents`.
- **Agent scaffolding** creates and validates a complete package before one atomic rename.
- **Declarative `config.yaml`** registers schemas, executor, model, budget, tools, eval data, and delegation without app-level wiring.
- **`AgentExecutionResult.completed/waiting/failed`** construct one valid outcome without manual status-field combinations.
- **`HarnessDTO.to_payload()`** serializes stable JSON-compatible boundary data.
- **Example prompt adapters** translate convenient `--prompt` text into each example's typed schema.

What the template does not provide is equally important: there is no decorator that bypasses registry validation, no direct tool callable shortcut, no automatic eval approval, and no raw provider/ORM object escape hatch.

## Project structure

```text
service-app/
├── app/
│   ├── api/                 # routes, request/response DTOs, dependencies, SSE
│   ├── cli/                 # app-specific serve command only
│   ├── workers/             # independent runtime worker entrypoint
│   ├── main.py              # FastAPI factory and error/lifecycle wiring
│   ├── runtime.py           # composition root for public core seams
│   └── migrate.py           # controlled migration entrypoint
├── agents/
│   └── examples/            # basic, RAG, ticket, repo, and dev examples
├── configs/
│   ├── profiles/            # typed local/service profiles
│   └── policy/              # default YAML policy
├── eval-cases/
│   ├── drafts/              # review queue; never scored as approved
│   └── approved/            # human-reviewed application dataset
├── tests/                   # copied-template public-seam tests
├── docs/
│   ├── README.md / README.zh-CN.md
│   ├── ai-agent-guide.md / ai-agent-guide.zh-CN.md
│   ├── examples.md / examples.zh-CN.md
│   └── ...                  # application-specific operating guides
├── scripts/                 # bootstrap, eval, service smoke, and admin helpers
├── Dockerfile               # wheel-only API/worker image
├── docker-compose.yml
├── .env.example
├── .gitignore
├── Makefile
├── pyproject.toml
├── README.md
└── README.zh-CN.md
```

## Module design

| Area | Responsibility and boundary |
|---|---|
| `app/main.py` | create one FastAPI app, install routers/error mapping, inject dependencies, and own component shutdown |
| `app/runtime.py` | bind settings, storage, registry, policy, events, queue, approvals, eval, and delegation through public core seams |
| `app/api` | authenticate, validate, convert DTOs, and call services; never own ORM sessions or business Agent logic |
| `app/cli` | provide `serve`; reuse the core CLI for everything else |
| `app/workers` | consume durable queue messages and restore execution from PostgreSQL truth |
| `agents/<agent>` | own business executor, schemas, allowed tools, config, and agent-specific eval cases |
| `configs/profiles` | describe environment topology and provider choices; no committed secret values |
| `configs/policy` | declare actions that allow, deny, or require approval |
| `eval-cases` | separate drafts from human-approved evidence |
| `scripts` | provide copied-project bootstrap and end-to-end verification without depending on repository source paths |

Vendor SDK imports belong in `agent_harness` adapters or an explicitly approved integration boundary. Template app code and business agents depend on provider-neutral DTOs, protocols, facades, repositories, and UoW seams.

Every run record and its evidence must preserve `tenant_id`, `agent_id`, and `run_id`; request/trace IDs add correlation but do not replace those three ownership keys.

## Configuration design

Configuration merge order is:

```text
profile YAML
  → agent YAML
  → .env
  → trusted *_FILE secret
  → process environment
  → explicit override
```

Environment variables use double underscores for nested fields, for example:

```bash
export AGENT_HARNESS_STORAGE__DSN="$STORAGE_DSN"
export AGENT_HARNESS_MODEL__PROVIDER=fake
export AGENT_HARNESS_SERVICE__API_PROCESS__ENABLED=false
```

Direct value and matching `_FILE` value are mutually exclusive. Secret files must be trusted absolute, regular, non-symlink files inside the configured secret root. Configuration failures are structured and fail before external connections or application startup.

## Example agents

| Agent | Demonstrates | Default safety behavior |
|---|---|---|
| `examples.rag_assistant` | local retrieval, citations, trust-preserving context, fake model | no source produces an honest `no_source` result |
| `examples.ticket_triage` | typed classification and confidence | ambiguous input becomes `unknown` / `needs_review` |
| `examples.repo_analyst` | workspace file read/search/list through `ToolRegistry` | no shell; path escape denied; large result uses `artifact_ref` |
| `examples.dev_assistant` | constrained file/shell tools, policy, HITL continuation | dangerous action waits for approval; deny has zero target side effects |

Commands, inputs, expected outputs, and eval boundaries are documented in [`docs/examples.md`](docs/examples.md).

## Development and testing

For an application-only change:

```bash
make quality
make test
make eval
make smoke-local
```

Run `make smoke-service` when the change affects migration, PostgreSQL, Redis, DBOS, service authentication, API/worker separation, queue recovery, approval continuation, or shared event/checkpoint evidence.

Inside the source repository, also run the root gates because they verify core/template import boundaries and the full contract suite:

```bash
cd ../..
make quality
make test
```

When adding or changing an endpoint, update `API-Contract.md` first and add a local runtime OpenAPI drift test. Do not rely on Swagger rendering alone as contract proof.

## Contributing

For a copied application:

1. Keep business behavior under `agents/*` and application composition under `app/*`.
2. Add typed schemas and public-seam tests for each behavior and failure path.
3. Keep `eval-cases/drafts` separate from `eval-cases/approved`; promotion requires human review.
4. Record deployment/provider choices in your own `docs/` and ADRs.
5. Never commit `.env`, `.agent-harness`, database files, traces, tokens, or provider payloads.

For an upstream template contribution:

1. Prove it works after a real copy-out with no repository source path or root `PYTHONPATH`.
2. Do not add member-level `workspace = true` or fixed `cd ../..` assumptions.
3. Keep `agent-harness-service` limited to `serve`; management logic belongs in the core CLI.
4. Preserve English/Chinese README parity for changed commands and behavior.
5. Run root quality/test gates and the relevant local/service smoke.

## Service profile

Run the complete real-dependency verification with:

```bash
make smoke-service
```

The script builds or consumes the core wheel, copies the template outside the workspace, and starts PostgreSQL, Redis, migration, API, and worker using the copied project only. It proves authenticated HTTP enqueue, worker pickup/reclaim, DBOS recovery, shared PostgreSQL checkpoint/event evidence, SSE resume, approval continuation, deny-without-continuation, and scoped cleanup.

This command is a verification harness, not a production deployment recipe. By default it removes its containers, network, volume, temporary credentials, queue namespace, and copied workspace.

To retain only the named PostgreSQL volume for diagnosis:

```bash
SERVICE_APP_KEEP_DATA=1 make smoke-service
```

The script still removes containers, network, temporary credentials, Redis namespace, and workspace files, then prints the exact `docker volume rm` command.

## Troubleshooting

### Required uv version mismatch

The source workspace and release wrappers accept uv `>=0.11.29,<0.12`; select `0.11.29` when reproducing the current CI environment. Release artifacts record their actual uv patch. The copied template keeps an exact `agent-harness` dependency matching its project version, while external dependencies use bounded ranges and the source `uv.lock` keeps the reviewed exact resolution.

### Copied project cannot resolve `agent-harness`

Run `make bootstrap AGENT_HARNESS_SOURCE=/absolute/path/to/wheel-or-source`. Do not add a public-index fallback unless your organization deliberately publishes the package and you explicitly enable `AGENT_HARNESS_ALLOW_INDEX=1`.

### Missing fingerprint key

Export `AGENT_HARNESS_BUDGET__FINGERPRINT_KEY` or configure its trusted `_FILE` counterpart before doctor, migration, API, worker, or run composition. Do not rotate it for an existing state database without a planned migration.

### Migration required

Run `app/migrate.py` with the same profile, profiles directory, and `STORAGE_DSN` used by the process. A database at a different path does not count.

### Agent is not listed

Check dotted lowercase `agent_id`, schema references, `executor: agent:executor`, package `__init__.py` files, and the resolved agents root. The registry rejects duplicates and invalid siblings as one unit.

### API starts but a run fails

Read run detail and events first. Then check policy, input guardrail, selected profile, storage migration, and executor output. In service profile also check Redis, worker readiness, and PostgreSQL.

### Tool or file access is denied

Inspect the Agent allowlist, `WorkspacePolicy`, `.agentignore`, shell allow/deny lists, current identity, policy decision, and approval record. Never call the underlying file system or subprocess directly to bypass the result.

### Port 8000 is already in use

Override the Make variable:

```bash
make dev PORT=8010
```

## Security boundaries

- User, retrieval, MCP, and tool content is untrusted input.
- Service identity comes from bearer/API-key verification, not request-body tenant fields.
- Tool calls go through schema validation, allowlist, workspace policy, `PolicyEngine`, redaction, audit, and artifact handling.
- A raw resume token cannot approve a dangerous action; use the approvals CLI/API.
- Local evidence must commit before optional observability provider fan-out.
- Eval detectors write drafts only; approved cases require a human review path.
- Provider SDK objects, ORM sessions, credentials, and raw errors must not cross public DTO boundaries.

## Further documentation

- [`docs/README.md`](docs/README.md): copied-app documentation map.
- [`docs/examples.md`](docs/examples.md): example agents, inputs, outputs, and eval commands.
- Source-repository [architecture](../../docs/architecture/README.md), [extension guide](../../docs/extension-guide.md), [adapter contracts](../../docs/adapter-contracts.md), [context/trust boundary](../../docs/context-and-trust-boundary.md), [security policy](../../docs/security-policy.md), [eval/observability loop](../../docs/eval-observability-loop.md), [release process](../../docs/release-process.md), and [ADRs](../../docs/adr/0001-p0-service-boundaries.md).

Repository-level links do not travel with a standalone template copy. Record application-specific deployment, provider, data, privacy, and recovery decisions in the copied project's own `docs/`.

## License and release boundary

The core repository is Apache-2.0. A copied application must choose and record its own license, privacy requirements, dependency/SBOM policy, model/data licensing, and release process.

`make dev` is a development server. `make smoke-service` is an end-to-end verification harness. Neither is a production deployment or proof of hosted runner, protected environment, artifact service, provider, or registry configuration.
