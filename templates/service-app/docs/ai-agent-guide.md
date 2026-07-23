# Use an AI / Agent to work on this project

[English](ai-agent-guide.md) | [简体中文](ai-agent-guide.zh-CN.md)

This is an ordinary, opt-in project guide. It does not automatically configure an AI tool or impose directory-level instructions. Give the AI this file as a link, paste its contents, or explicitly ask the AI to read it before working.

## The shortest way to use it

From a copied service-app project, send your AI / Agent this message:

```text
Read docs/ai-agent-guide.md first, then inspect this project and complete the task below.
Follow the guide's source-of-truth, architecture, security, validation, and handoff rules.
Do not commit, push, deploy, publish, use production credentials, or call a real provider
unless I separately authorize that exact action.

Task: <describe what you want>
Acceptance criteria: <describe what success looks like>
```

Use the more specific initialization and feature prompts at the end of this guide when you already know the scope.

## What the AI should inspect first

The AI should preserve current files and user changes, then read the smallest relevant set:

- `README.md`, `pyproject.toml`, `Makefile`, `.env.example`, and `.gitignore`;
- the selected `configs/profiles/*.yaml`;
- the relevant `agents/<namespace>/<name>/` package and eval cases;
- `app/api/`, `app/runtime.py`, or `app/workers/` only when the task crosses those boundaries;
- relevant tests under `tests/`.

When the template is still inside the Agent Harness Layer source repository, repository-only truth sources may also exist: `Product-Spec.md`, `DEV-PLAN.md`, `API-Contract.md`, an active OpenSpec change, or an ADR. Read the relevant requirements when present. Do not assume those files exist in a standalone copy, and do not invent them merely because this guide names them. If two truth sources conflict, report the exact conflict before changing behavior.

## Non-negotiable project boundaries

- Plan the work in small, independently verifiable outcomes before editing.
- Do not overwrite an existing project, Agent directory, or unrelated user change.
- Update applicable user or contract documentation before a behavior change. Keep English and `.zh-CN.md` guides aligned when both exist.
- Business code under `agents/*` uses public `agent_harness` DTOs, protocols, registries, facades, and repositories. It does not import vendor SDKs, access ORM sessions, or bypass runtime/policy boundaries.
- Preserve `tenant_id`, `agent_id`, and `run_id` through run, event, usage, audit, and artifact evidence. Request/trace IDs add correlation; they do not replace these identities.
- Grant only the required tool, retrieval, delegation, network, filesystem, and HITL authority. Leave unused capabilities disabled.
- Never reveal or commit secrets in documentation, examples, logs, fixtures, diffs, or handoff text.
- Commit, push, deployment, publication, real-provider calls, production credentials, and registry side effects each require explicit user authorization.
- Validate the changed scope with actual evidence. Documentation-only work needs documentation checks, not an unrelated full test suite.

## Initialize a copied service-app

### 1. Determine the package source

First confirm the current directory is the intended target and inspect `git status` if Git is present. Determine whether the project is the template inside the Agent Harness Layer workspace or an independent copy.

Inside the source workspace:

```bash
make bootstrap
```

An independent copy needs a trusted `agent-harness` wheel, sdist, source directory, or private index. Prefer an explicit local source:

```bash
make bootstrap \
  AGENT_HARNESS_SOURCE=/absolute/path/to/agent_harness-0.1.0-py3-none-any.whl
```

Use a trusted private index only when the user has selected and configured it:

```bash
make bootstrap AGENT_HARNESS_ALLOW_INDEX=1
```

Do not silently install a public same-name package. If no trusted source is available, stop and report the accepted source types.

### 2. Prepare local state

Use the local profile and fake model unless the task explicitly requires another environment. Generate a fingerprint key for this state database without printing or committing it, configure local paths, and migrate:

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

An ignored `.env` may contain environment-specific overrides when persistence is requested. Never add a default fingerprint key to `.env.example` or Git. Reuse the same key for the lifetime of the corresponding database.

### 3. Prove the local path

```bash
make smoke-local
make run-basic
```

Record the actual exit status and emitted `run_id`. Start HTTP with `make dev` only when requested. Local/fake evidence does not prove the service profile, external telemetry, a real provider, hosted CI, or production deployment.

## Implement an Agent or application feature

### 1. Convert the request into a contract

Identify:

- user-visible behavior;
- typed inputs and outputs;
- normal, failure, ambiguity, and security paths;
- affected public interfaces;
- evidence needed to prove acceptance.

Ask the user when a missing decision changes the public contract, security boundary, destructive action, or external cost. Otherwise use and report the smallest reversible assumption.

### 2. Map the work to five layers and two wings

| Area | Default decision |
|---|---|
| Access | Reuse CLI/HTTP, authentication, typed requests, OpenAPI, SSE, and error envelopes. Add routes only for real protocol needs. |
| Runtime | Register configuration and implement `AgentExecutor`; reuse runs, checkpoints, approval, idempotency, budget, and delegation. |
| Engine | Put typed schemas and business behavior in the Agent package. This is usually the main implementation area. |
| Tools | Keep `tool_allowlist` empty unless an external action is necessary; then add typed registry, policy, workspace, and HITL boundaries. |
| Infra | Start local/fake; add storage, queue, retrieval, providers, or business adapters only when required. |
| Eval Gate | Capture behavior as draft and require human review before `approved`; automation never self-approves. |
| Observability | Preserve local canonical events, usage, and audit first; external telemetry is optional and degradable. |

Future `Graph Nodes`/`GraphState` and independent gateways are not prerequisites. The architecture diagram's conceptual `@agent.tool` label means the public `ToolRegistry`; do not invent a decorator that bypasses registry, policy, or approval.

### 3. Create or change an Agent

For a new Agent:

```bash
uv run agent-harness scaffold agent my_team.my_agent
```

Project-root discovery belongs only to `scaffold agent`. In a copied application, `agents list` and `run` still need `--agents-dir ./agents`:

```bash
uv run agent-harness agents list \
  --profile local \
  --profiles-dir ./configs/profiles \
  --agents-dir ./agents \
  --storage-dsn "$STORAGE_DSN"

uv run agent-harness run my_team.my_agent \
  --profile local \
  --profiles-dir ./configs/profiles \
  --agents-dir ./agents \
  --storage-dsn "$STORAGE_DSN" \
  --prompt '<representative input>'
```

Implement in this order:

1. `schemas.py`: typed input/output and validation boundaries;
2. `agent.py`: module-level `executor` returning exactly one `AgentExecutionResult` state;
3. `config.yaml`: stable identity, schema/executor paths, model, budget, disabled-by-default permissions, and approved eval path;
4. `tools.py`: only required typed tools, with policy/HITL/workspace controls;
5. `evals/drafts/`: normal, failure, ambiguity, and security cases; promote to `approved` only after human review.

Do not wire each Agent into a route or create another scheduler. `AgentRegistry.load_from_directory()` is the declarative convenience layer and still enforces configuration, schema, executor, runtime, policy, and storage contracts.

### 4. Treat cross-cutting changes explicitly

- **HTTP/API:** change routes, schemas, and API documentation together; check OpenAPI drift for a public surface change.
- **Tools or dangerous actions:** add allow/deny evidence and prove approval occurs before any side effect.
- **Retrieval/RAG:** retain `source_ref`, `trust_level`, and injection boundaries; test no-hit and untrusted content.
- **Storage/queue/service profile:** add forward migration and recovery evidence; run service smoke only when the change requires those dependencies.
- **Multi-Agent:** declare `delegation_edges`; use registry/policy/shared parent budget instead of private recursive calls.
- **Provider integration:** implement behind public adapters, record usage/cost/latency, and keep business Agents vendor-neutral.

### 5. Select the smallest sufficient validation

| Change | Minimum evidence |
|---|---|
| Documentation only | Local links/anchors, code-fence syntax, and relevant documentation contract tests; no unrelated full suite |
| Agent behavior/schema/config | Targeted tests, registry listing, one representative CLI run, and relevant approved eval |
| Tool/retrieval/policy | Targeted allow/deny/failure/HITL tests plus local event/audit inspection |
| HTTP/API | Targeted route tests and OpenAPI contract/drift checks |
| App/runtime integration | `make quality`, targeted tests, and `make smoke-local` |
| PostgreSQL/Redis/worker/migration | Relevant migration/integration tests and `make smoke-service` |

Use `make eval` for approved regression data. `no-approved-cases` means no approved evidence exists; it is not a passing evaluation. A heavy service or full-repository check needs a scope reason.

## Required handoff

The AI should return:

1. outcome and user-visible behavior;
2. changed files and why each changed;
3. exact validation commands and results;
4. unverified environments or side effects;
5. decisions still required from the user.

It must not describe local/fake evidence as proof of service, hosted, provider, registry, or production behavior.

## Copyable prompts

### Initialize this project

```text
Read docs/ai-agent-guide.md and initialize this copied Agent Harness service-app.

Trusted agent-harness source: <absolute wheel/sdist/source path or approved private index>
Target profile: local
Initial Agent: <none or namespace.name>

Preserve existing files. Do not use production credentials or commit, push, deploy,
publish, or call a real provider. Bootstrap the trusted package, create ignored local
state, migrate it, run smoke-local and run-basic, and report exact commands/results and
blockers. If an Initial Agent is named, scaffold it but do not invent behavior beyond
the acceptance criteria I provide.
```

### Implement a feature

```text
Read docs/ai-agent-guide.md and implement this feature:

Goal: <user-visible outcome>
Inputs/outputs: <public data contract>
Acceptance criteria: <normal, failure, and security behavior>
Allowed external systems: <none or explicit systems>
Constraints: <compatibility, security, performance, or scope>

Inspect current code and relevant truth sources first. Map the change to the five layers
and two wings, use public agent_harness seams, keep permissions minimal, add targeted
regression/eval evidence, and run only the smallest sufficient validation. Do not commit,
push, deploy, publish, use production credentials, or call a real provider unless I
separately authorize it. Report changed files, exact results, assumptions, and unverified
boundaries.
```
