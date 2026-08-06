# Agent Harness Layer

[English](README.md) | [简体中文](README.zh-CN.md)

Agent Harness Layer is a Python core package and copyable backend service template for building governed agent applications. It supplies the parts that demo-style agent projects usually leave until too late: typed configuration, identity and policy, durable runs and approvals, constrained tools, retrieval, observability, evaluation, packaging, and release gates.

This repository is for two audiences:

- **Agent application developers** copy and extend [`templates/service-app`](templates/service-app/README.md).
- **Scaffold maintainers** evolve the reusable `agent_harness` package, template, adapters, contracts, and verification pipeline.

The local profile runs with SQLite, an in-memory queue, local JSONL evidence, and a fake model. It does not require a real model key or an observability SaaS account. The service profile uses PostgreSQL, Redis, a migration process, FastAPI, and a separate runtime worker.

## What it does

The current repository provides:

- a buildable `agent-harness` wheel and sdist;
- a copyable FastAPI, Typer, worker, and Docker Compose service template;
- typed profile, agent, `.env`, process-environment, and secret-file configuration;
- tenant-aware identity, policy checks, input guardrails, and HITL approvals;
- durable run, checkpoint, resume, idempotency, queue, and event contracts;
- constrained file, shell, and MCP tool boundaries;
- local and PostgreSQL retrieval with optional PGroonga/pgvector adapters;
- local-first events, telemetry adapters, and a trace-to-eval workflow;
- four runnable example agents and an atomic agent scaffolding command;
- quality, test, eval, smoke, build, license, CI-contract, and release-preview gates.

It is not a hosted agent platform, a frontend administration product, or proof that your production environment has been deployed. Hosted GitHub/GitLab runners, protected remote environments, external artifact services, real provider integrations, and real registry publication remain `hosted-unverified` in this checkout.

## Choose your path

| Goal | Start here |
|---|---|
| Run an agent application for the first time | [Service-app first use](templates/service-app/README.md#first-use-local-profile) |
| Create a business agent | [Create an agent](templates/service-app/README.md#create-an-agent) |
| Ask an AI / Agent to initialize or implement a feature | [AI / Agent project guide](templates/service-app/docs/ai-agent-guide.md) |
| Apply the five-layer, two-wing architecture | [Build an Agent guide](docs/building-an-agent.md) |
| Call the HTTP API | [Service-app HTTP API](templates/service-app/README.md#http-api) |
| Use the Python package | [Python API](#python-api) |
| Understand architecture and safety boundaries | [Module design](#module-design) and [Deep documentation](#deep-documentation) |
| Modify the reusable scaffold | [Developer guide](#developer-guide) and the full [contributor guide](CONTRIBUTING.md) |

## Hand project work to an AI / Agent

The copied template carries an ordinary, opt-in [AI / Agent project guide](templates/service-app/docs/ai-agent-guide.md). Give the AI that link or copy this starter prompt; the guide itself does not automatically configure a tool or impose directory-level instructions:

```text
Read templates/service-app/docs/ai-agent-guide.md first, then inspect this project and
complete this task: <task and acceptance criteria>.

Follow the guide's architecture, security, validation, and handoff rules. Do not commit,
push, deploy, publish, use production credentials, or call a real provider unless I
separately authorize that exact action.
```

After copying `templates/service-app` into its own project, the path becomes `docs/ai-agent-guide.md`.

## Build an Agent with five layers and two wings

You do not implement seven independent systems for every Agent. The template already supplies most of Access and Runtime; business code primarily implements Engine, adds Tools and Infra only when required, and uses Eval plus Observability across the whole lifecycle.

| Architecture area | What an Agent developer actually does |
|---|---|
| Access | reuse the template CLI/HTTP API, authentication, typed requests, OpenAPI, and SSE |
| Runtime | register `config.yaml` and implement the `AgentExecutor` contract; reuse runs, checkpoints, approvals, and delegation |
| Engine | define `schemas.py`, `agent.py`, model selection, budget, and business behavior |
| Tools | add only the typed tools, allowlist, workspace policy, and HITL rules the Agent needs |
| Infra | start with local/fake dependencies; configure storage, queue, retrieval, providers, and secrets when the use case requires them |
| Eval Gate | turn reviewed behavior into approved regression evidence; automated signals stay in drafts |
| Observability | inspect local events, usage, and audit first; external telemetry providers are optional fan-out |

See [Use five layers and two wings to build an Agent](docs/building-an-agent.md) for the request flow, a minimal implementation path, complexity choices, and a complete `support.triage` mapping. The diagram's Graph Nodes/GraphState and independent gateways are future extension points, not prerequisites. Its conceptual `@agent.tool` label maps to the current public `ToolRegistry`; this project does not expose a decorator that bypasses registry, policy, or approval.

## Prepare the environment

Required for local development:

- macOS or Linux;
- Git;
- GNU Make;
- Python `>=3.12`;
- uv `>=0.11.29,<0.12` for local development and release wrappers. CI currently selects `0.11.29`; each release artifact records the actual supported patch it used.

Verify before running repository commands:

```bash
python3 --version
uv --version
git --version
make --version
```

If uv is missing or outside the supported range, install the current concrete CI version with the [official versioned installer](https://docs.astral.sh/uv/getting-started/installation/), or use your package manager's equivalent version-selection mechanism:

```bash
curl -LsSf https://astral.sh/uv/0.11.29/install.sh | sh
uv --version
```

The three `pyproject.toml` files declare bounded compatibility ranges for external dependencies. The root and service template deliberately keep `agent-harness==0.1.0` because they are released from this repository as one versioned set. `uv.lock` remains the exact, reviewed resolution: normal `uv sync --frozen` and `uv lock --check` do not upgrade packages. Start dependency upgrades separately with `uv lock --upgrade` and review the resulting lock diff.

The service profile additionally requires Docker with Compose v2. See the [official Docker Compose installation guide](https://docs.docker.com/compose/install/).

## First use

### 1. Install the workspace

From the repository root:

```bash
uv sync
```

#### PyCharm and Pyright

This repository requires the entire uv workspace to use the root `.venv`; member projects do not
create separate environments. The committed root `[tool.pyright]` therefore declares
`venvPath = "."` and `venv = ".venv"` explicitly. Current PyCharm needs explicit `venvPath` and
`venv` settings to resolve dependencies reliably; these particular values come from this
repository's root `.venv` convention. The template member uses `venvPath = "../.."` to select that
same root environment. The first bootstrap of a copied project normalizes the value to `.`.
The template member also repeats `agent-harness = { workspace = true }` in its own
`tool.uv.sources`, because PyCharm may not apply the root source mapping to that member. A copied
project's first bootstrap replaces or removes this workspace-only source before dependency sync.

In PyCharm, enable `Use pyproject.toml-based project model`, run
`Sync Project with pyproject.toml`, and confirm that the project SDK points to the root `.venv`.

After editing the TOML, syncing the PyCharm project model may not restart an already-running
Pyright server. Restart Pyright from the status-bar language-services menu, or reopen the project,
to apply the new environment configuration immediately.

Root `make pyright` and `make quality` verify that the active uv environment follows this root
`.venv` convention, preventing a misleading pass against two different environments.

##### Override the root `.venv` convention

If this repository must place its virtual environment elsewhere, configure both uv and a
machine-local `pyrightconfig.json`. For example:

```bash
export UV_PROJECT_ENVIRONMENT="/absolute/path/to/python envs/agent-harness-layer"
```

Create this file at the repository root:

```json
{
  "extends": "./pyproject.toml",
  "venvPath": "/absolute/path/to/python envs",
  "venv": "agent-harness-layer"
}
```

Then run `uv sync` and `make quality`, point the PyCharm project SDK to the Python in that same
environment, resync the project model, and restart Pyright. `pyrightconfig.json` takes precedence
over the committed `[tool.pyright]`; the Make targets neither generate nor rewrite it, and the root
`.gitignore` ignores it by default. Do not use `~` or environment variables in Pyright path fields
because Pyright does not expand them.

For a copied, independent project, follow the
[template guide](templates/service-app/README.md#custom-virtual-environment-path); that guide is
included in every copy. See the official
[Pyright configuration precedence](https://github.com/microsoft/pyright/blob/main/docs/configuration.md)
and [uv project environment](https://docs.astral.sh/uv/concepts/projects/layout/) documentation.

### 2. Verify the offline development surface

```bash
make quality
make test
make smoke-local
make eval
```

These commands use the local/fake path and do not require a real model API key. `make smoke-local` creates isolated state, injects an ephemeral budget fingerprint key, migrates its own database, and exercises the packaged CLI.

### 3. Start the actual service template

Repository checks do not start a long-running API. To use the application, continue with the template:

```bash
cd templates/service-app
make bootstrap

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

make dev
```

Then open:

- health: `http://127.0.0.1:8000/api/v1/health`
- Swagger UI: `http://127.0.0.1:8000/docs`
- Redoc: `http://127.0.0.1:8000/redoc`
- OpenAPI JSON: `http://127.0.0.1:8000/openapi.json`

Keep the fingerprint key stable for the lifetime of the same state database. It is a budget request fingerprint secret, not a model API key. Never commit it.

## Usage guide

### Controlled non-streaming model runtime

The committed local and service profiles explicitly select `fake_default`; ordinary quality,
test, eval, and local smoke commands remain offline even if ambient `OPENAI_*` or proxy variables
exist. A real text deployment is opt-in and must use branded `AGENT_HARNESS_MODEL__...` typed
configuration. It references a credential, exact endpoint policy, and versioned model catalog.
Agent YAML may only narrow the deployment/model allowlist, and a request may narrow it again;
neither surface can choose a URL, credential, SDK client, or model outside the frozen intersection.

The runtime freezes the route before policy, budget, client, or network side effects. Each root run
persists a versioned route snapshot and reuses its endpoint path, catalog, attempt bounds, and price
identity during recovery. The async provider path applies one total deadline, bounded retry, a
process-local per-deployment bulkhead, durable side-effect marking, and conservative `unknown`
handling. A real failure never falls back to `fake`.

Use [the service-app environment example](templates/service-app/.env.example) for field names and
secret-file wiring. `make smoke-live-model` is intentionally inert without separate current-session
authorization: it reports `model-live-smoke/v1 status=hosted-unverified`,
`provider_called=false`, and exits 0. An authorized run also requires explicit opt-in, an isolated
branded credential, and a trusted endpoint. It executes through the normal orchestrator, policy
audit, shared budget reservation, usage evidence, and provider lifecycle.
One deployment may contain an ordered list of fallback models. The router selects them only before
provider side effects from frozen budgets and never replays a real failure automatically. Use
`--secret-root` when the isolated `_FILE` directory is not the default `/run/secrets`.

The complete profile-merge, Agent-policy, catalog-digest, configuration-check, and authorized live
smoke procedure is in the service template's
[controlled real text deployment guide](templates/service-app/README.md#controlled-real-text-deployment).
Only model-catalog identity fields participate in `model_catalog_digest`; changing a base URL,
allowed origin, endpoint policy, or credential does not require a catalog digest update. The runtime
derives a separate endpoint-policy digest from those endpoint-bound fields.

Explicit cross-deployment fallback uses Agent `fallback_routes` to freeze an ordered candidate
chain. Every candidate has its own endpoint, credential, catalog, bulkhead, and budget. The chain
advances only with durable `client_not_started` or endpoint-policy-bound
`trusted_business_not_started` proof; unknown results, untrusted responses, usage, text, or the
first delta fence every successor. `make smoke-live-model-failover` is zero-call and
`hosted-unverified` by default. A real probe additionally requires all five authorization flags and
`AGENT_HARNESS_LIVE_MODEL_FAILOVER_DEPLOYMENTS=<primary>,<secondary>`.

### Repository commands

| Command | What it proves | Additional requirements |
|---|---|---|
| `make quality` | Ruff format/lint, Pyright, import boundaries | local toolchain |
| `make test` | unit, contract, and offline integration behavior | local dependency set |
| `make eval` | approved fake-model eval cases | no real provider key |
| `make smoke-local` | isolated SQLite/in-memory/local-JSONL runtime | no external services |
| `make smoke-live-model` | truthful opt-in status or one governed completion | separate authorization, isolated branded credential, trusted endpoint |
| `make smoke-live-model-failover` | validate the two-deployment artifact or execute one governed chain | separate authorization, isolated credentials/endpoints, trusted not-started fixture |
| `make smoke-service` | copied-template API/worker recovery through PostgreSQL and Redis | Docker Compose |
| `make build` | local wheel, sdist, and checksums | does not publish |
| `make license-check` | dependency/license inventory, NOTICE, vendoring, and image identity policy | not legal advice or a full SBOM |
| `make release-dry-run` | ignored local release preview | no tag, push, or publication |

### Core CLI

The core command is `agent-harness`:

```text
agent-harness doctor
agent-harness agents list
agent-harness run <agent_id>
agent-harness events stream <run_id>
agent-harness tools list|call
agent-harness policy check
agent-harness approvals list|approve|deny
agent-harness eval draft|list|approve|run|scores|experiment
agent-harness scaffold agent <agent_id>
agent-harness migrate-local-state
```

The template adds only `agent-harness-service serve`. It does not duplicate core business commands.

See [the service-app CLI guide](templates/service-app/README.md#cli) for complete local-profile examples.

### HTTP API

The service template exposes `/api/v1` routes for agents, runs, JSON events, SSE events, approvals, policy checks, eval cases, eval runs, eval experiments, and health. The shortest local-profile flow is:

```bash
curl -sS http://127.0.0.1:8000/api/v1/agents

curl -sS \
  -H 'Content-Type: application/json' \
  -d '{"input": {}, "idempotency_key": "first-run"}' \
  http://127.0.0.1:8000/api/v1/agents/examples.basic/runs
```

Use the returned `run_id` to read detail and events:

```bash
curl -sS "http://127.0.0.1:8000/api/v1/runs/$RUN_ID"
curl -sS "http://127.0.0.1:8000/api/v1/runs/$RUN_ID/events?after_seq=0"
curl -N \
  -H 'Accept: text/event-stream' \
  -H 'Last-Event-ID: 0' \
  "http://127.0.0.1:8000/api/v1/runs/$RUN_ID/events/stream"
```

Local profile routes use the default local identity. The service profile requires `Authorization: Bearer <token>` and obtains tenant/user identity from the verifier, not from the request body. Consult [`API-Contract.md`](API-Contract.md) for field-level schemas, exact status codes, idempotency, visibility, and recovery semantics.

## Python API

The top-level `agent_harness` package intentionally exports only `__version__`. Import stable capabilities from explicit submodules so dependency direction remains visible.

### Load typed configuration

```python
from pathlib import Path

from agent_harness.config import load_settings

settings = load_settings(
    profile="local",
    profiles_dir=Path("templates/service-app/configs/profiles"),
)
print(settings.profile)
```

Merge precedence is profile YAML → agent YAML → `.env` → trusted secret file → process environment → explicit overrides. Invalid configuration fails before the API, worker, migration, or run performs external side effects.

### Discover agents

```python
from pathlib import Path

from agent_harness.registry import AgentRegistry

registry = AgentRegistry.load_from_directory(Path("templates/service-app/agents"))
for descriptor in registry.list_agents():
    print(descriptor.agent_id, descriptor.description)
```

`load_from_directory()` validates all descriptors, schema references, and executor references before publishing a usable registry. It rejects a partially valid registry instead of silently skipping broken agents.

### Implement an executor

```python
from agent_harness.runtime import (
    AgentExecutionContext,
    AgentExecutionRequest,
    AgentExecutionResult,
    ApprovalGrant,
)


class ExampleExecutor:
    async def run(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
    ) -> AgentExecutionResult:
        del context
        return AgentExecutionResult.completed({"echo": request.input})

    async def resume(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
        grant: ApprovalGrant,
    ) -> AgentExecutionResult:
        del request, context, grant
        return AgentExecutionResult.failed("this agent has no approval continuation")


executor = ExampleExecutor()
```

The registry loads the module-level `executor` referenced by the agent's `config.yaml`. Runtime orchestration, identity, policy, checkpoint, event, and recovery behavior stay outside the business executor.

### Stable payloads and helpers

Public DTOs inherit `HarnessDTO`; `to_payload()` returns a JSON-compatible dictionary with `None` fields omitted:

```python
from agent_harness.contracts import HarnessDTO


class Output(HarnessDTO):
    result: str
    optional_ref: str | None = None


payload = Output(result="ok").to_payload()
assert payload == {"result": "ok"}
```

Other focused helpers include `retrieval_result_to_context_fragment()`, `retrieval_results_to_context_fragments()`, `merge_rrf()`, `mcp_tools_from_client()`, `build_execute_message()`, and `build_resume_approval_message()`. They normalize stable boundaries; they do not bypass policy, identity, trust, approval, or persistence checks.

## Ergonomic layers and “syntax sugar”

The project has convenience layers, but deliberately avoids a magic decorator or one-call DSL that hides governance:

- `make ...` targets are short, stable wrappers around repository scripts and CLI commands.
- `agent-harness scaffold agent support.triage` atomically creates a safe agent package, typed schemas, config, empty tool/delegation permissions, and a draft eval case.
- `AgentRegistry.load_from_directory()` replaces manual YAML enumeration and dynamic import handling.
- `AgentExecutionResult.completed(...)`, `.waiting(...)`, and `.failed(...)` are typed constructors for mutually exclusive outcomes.
- `HarnessDTO.to_payload()` is the serialization shortcut for public boundaries.
- `config.yaml` is declarative registration syntax: it selects schemas, executor, model policy, budget, tool allowlist, eval dataset, and delegation edges without app-level wiring.
- CLI `--prompt` is a convenience input for interactive runs. A business agent still validates its typed input; prompt translation is not a universal schema bypass.

The convenience layer always ends at the same registry, runtime, policy, storage, and event seams. If a shortcut would skip one of those boundaries, it is not supported sugar; it is a bug.

## Project structure

```text
agent-harness-layer/
├── packages/agent-harness/       # buildable provider-neutral core package
├── templates/service-app/        # copyable FastAPI/CLI/worker application
│   └── agents/examples/          # maintained runnable examples
├── examples/                     # reserved package-level example area
├── docs/                         # architecture, extension, security, eval, release, ADRs
├── scripts/                      # quality, smoke, build, compliance, and release tooling
├── tests/                        # repository contract and integration evidence
├── compliance/                   # dependency/license policy and observations
├── openspec/                     # durable behavior specifications and archived changes
├── Product-Spec.md               # product-level source of truth
├── API-Contract.md               # field-level API/CLI/module contract
├── DEV-PLAN.md                   # phased implementation and evidence plan
├── pyproject.toml                # uv workspace and pinned toolchain contract
├── Makefile                      # stable local/CI entrypoints
├── LICENSE
├── NOTICE
├── README.md                     # English entrypoint
└── README.zh-CN.md               # Chinese entrypoint
```

The root `examples/` directory is reserved and is not the runnable template example location. Use `templates/service-app/agents/examples/` for the maintained agents.

## Module design

| Module | Design intent |
|---|---|
| `contracts`, `identity`, `config` | Stable validated data, trust markers, structured errors, and fail-closed startup inputs. |
| `registry`, `runtime`, `delegation` | Discover executors and coordinate run, checkpoint, queue, approval continuation, and parent/child lifecycle without business-code shortcuts. |
| `policy`, `approvals`, `auth`, `audit` | Keep authentication, authorization, review, and evidence separate while preserving tenant/run/request/trace correlation. |
| `tools`, `mcp`, `artifacts` | Validate names and schemas, enforce allowlists/workspace/policy, redact output, and externalize large results before side effects or context injection. |
| `models`, `embeddings`, `retrieval`, `context` | Keep provider calls neutral, preserve usage/budget/source/trust evidence, and assemble bounded context. |
| `events`, `observability`, `evals` | Commit local evidence first, then perform optional provider fan-out; drafts require human review before approved evaluation. |
| `storage`, `adapters` | Own SQLAlchemy repositories/UoW/migrations and isolate vendor SDK or driver imports from core contracts and business agents. |
| `templates/service-app/app` | Thin HTTP/CLI/worker composition and DTO conversion; no business agent logic. |
| `templates/service-app/agents` | Business executors, schemas, config, tools, and agent-specific eval cases. |

Today the service profile physically separates the API and runtime worker. Model/tool gateways, an event pipeline, and a storage service are future split points; current provider and repository seams remain in-process.

## Developer guide

Use the full [contributor guide](CONTRIBUTING.md) for the human/Agent workflow, file ownership,
mechanical rule sources, validation matrix, and Git/security limits.

Do not use this README as a second workflow specification. Follow the contributor guide's
source-of-truth order, including applicable synchronized OpenSpec specs and active deltas, and use
its minimum-sufficient validation and authorization rules.

Keep these dependency rules:

- `agent_harness/*` must not depend on templates or concrete example agents.
- `app/*` owns protocol entrypoints and composition, not business agent logic.
- `agents/*` use public `agent_harness` seams and do not import vendor SDKs or ORM sessions.
- vendor SDKs stay behind approved adapter/integration boundaries.
- `eval-cases/approved` is written only by a reviewed approval flow.
- delegation goes through `AgentRegistry`, `PolicyEngine`, and the runtime service.
- every run record and its evidence preserves `tenant_id`, `agent_id`, and `run_id`; request/trace IDs add correlation but do not replace those ownership keys.

Choose validation from the authoritative matrix in `CONTRIBUTING.md`; this README intentionally
does not define a second fixed command bundle. Report only the checks that actually ran and the
boundary each result proves.

## Contributing

The authoritative contribution workflow for both humans and Agents is in
[`CONTRIBUTING.md`](CONTRIBUTING.md), with a [Simplified Chinese edition](CONTRIBUTING.zh-CN.md).

1. Start from a focused issue or change contract; avoid mixing unrelated cleanup.
2. Update the relevant product/API/OpenSpec contract before behavior changes.
3. Add or update a test through a public seam: CLI, HTTP, module protocol, repository/UoW, event, or persisted boundary.
4. Preserve the package and vendor import boundaries.
5. Use Conventional Commit prefixes such as `feat:`, `fix:`, `docs:`, `refactor:`, or `chore:`.
6. Run the relevant verification commands and include exact results in the review description.
7. Do not commit `.env`, `.agent-harness`, database files, traces, credentials, or generated release previews.

Documentation-only contributions must still verify commands, internal links, language parity, and current-versus-future claims against the checkout.

## Troubleshooting

### uv rejects every command

If the error says the required uv version does not match, select a uv version in `>=0.11.29,<0.12`; use `0.11.29` when reproducing the current CI environment. Preview, formal build, and publish plans record their actual uv patch. The rejection occurs before project code runs.

### `config.invalid` or a missing fingerprint key

Export `AGENT_HARNESS_BUDGET__FINGERPRINT_KEY` before direct `doctor`, `agents list`, `run`, API, worker, or migration composition. Keep it stable for the same state database, but do not commit it.

### `storage.migration_required`

Run the template migration against the exact `STORAGE_DSN` you will use. Changing the environment variable without migrating the corresponding database does not initialize it.

### The API is healthy but runs do not progress

For local profile, inspect the run events and local JSONL path. For service profile, check migration completion, Redis reachability/consumer group, worker readiness, and PostgreSQL state. Health is a configuration/capability summary, not end-to-end run proof.

### An agent is missing from the registry

Check the dotted lowercase `agent_id`, `config.yaml`, schema references, module-level executor reference, and the selected `--agents-dir`. Registry loading fails as a whole when a descriptor is invalid or duplicated.

### A tool is denied

Check the agent tool allowlist, workspace normalization and `.agentignore`, identity permissions, policy decision, and approval state. Do not enlarge the workspace root or bypass `ToolRegistry` to make the call pass.

## Security notes

- Never place secrets in profile YAML, README examples, request bodies, trace/eval/audit payloads, or committed `.env` files.
- Use process environment or the trusted `_FILE` configuration boundary for deployment secrets.
- Treat user, retrieval, MCP, and tool content as untrusted until it passes context/guardrail handling.
- A raw resume token is not approval authority; approval-gated continuation must go through `ApprovalService`.
- Do not expose a remote `/api/v1/tools` route unless a future contract explicitly adds it.

See the [security policy](docs/security-policy.md) and [context/trust boundary](docs/context-and-trust-boundary.md).

## Deep documentation

| Question | Document |
|---|---|
| How should humans and Agents plan, isolate, validate, and hand off a change? | [Contributor guide](CONTRIBUTING.md) |
| Which layering, design-principle, and pattern-selection rules govern architecture changes? | [Engineering principles](docs/engineering-principles.md) |
| How do five layers and two wings become one working Agent? | [Build an Agent guide](docs/building-an-agent.md) |
| How does this project relate to Pydantic AI Harness and Agently? | [Framework positioning and capability comparison guide](docs/framework-positioning.md) |
| How can I hand project initialization or feature work to an AI / Agent? | [AI / Agent project guide](templates/service-app/docs/ai-agent-guide.md) |
| What runs today, and what may split later? | [Architecture and deployment boundaries](docs/architecture/README.md) |
| Where can I add an agent or capability? | [Extension guide](docs/extension-guide.md) |
| Which DTO/protocol/facade/repository/UoW boundaries are stable? | [Adapter contracts](docs/adapter-contracts.md) |
| How are untrusted inputs assembled and governed? | [Context and trust boundary](docs/context-and-trust-boundary.md) |
| How do identity, policy, approvals, secrets, and audit interact? | [Security policy](docs/security-policy.md) |
| How do traces become reviewed eval evidence? | [Eval and observability loop](docs/eval-observability-loop.md) |
| What is locally verifiable versus hosted-unverified? | [Release process](docs/release-process.md) |
| Why were the service, vendor, and Redis boundaries chosen? | [ADRs](docs/adr/0001-p0-service-boundaries.md) |

## License and release boundary

The project is licensed under Apache-2.0. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE). Runtime dependency policy and observations live under `compliance/`; run `make license-check` before adding or upgrading a dependency or runtime image.

`make build` creates local wheel/sdist artifacts and checksums. `make release-dry-run` creates a local ignored preview. Neither command publishes, pushes, tags, deploys, or proves hosted CI. Follow the [release process](docs/release-process.md) before any protected promotion or private-registry execution.
