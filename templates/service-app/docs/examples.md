# Four example Agents

[English](examples.md) | [简体中文](examples.zh-CN.md)

All four examples use the public `AgentRegistry -> RunOrchestrator -> AgentExecutor` path. The local profile uses a fake model, SQLite, and local JSONL with no real API key. They demonstrate extension points; they are not complete products and do not implement eval experiments, harness comparison, or automatic optimization.

## Run and evaluate

```bash
make run-rag
make run-ticket
make run-repo
make run-dev
make eval
```

Use `make eval-rag`, `make eval-ticket`, `make eval-repo`, or `make eval-dev` to run each approved dataset separately. Automated signals can write drafts only. Approved JSON requires human review, and `EvalRunner` scores approved cases only.

## Capabilities and boundaries

| Agent | Real validation path | Safe degradation |
|---|---|---|
| `examples.rag_assistant` | The query first passes `EmbeddingInvocationService` to record usage evidence, then `RetrievalProvider -> ContextFragment -> ContextAssemblyService -> ModelInvocationService`, returning citations and assembly trace | SQLite FTS5/BM25; no hit returns `no_source`; retrieval chunks are always `untrusted` |
| `examples.ticket_triage` | Typed schema, deterministic classification rules, fake-model evidence | Low confidence returns `unknown`, `needs_review=true` instead of inventing a category |
| `examples.repo_analyst` | Allowlisted file read/search/list through `WorkspacePolicy` and artifact store | Out-of-bound or `.agentignore` paths return `tool.workspace_denied`; long results inline a summary with `artifact_ref`; shell is unavailable |
| `examples.dev_assistant` | File/shell `ToolRegistry`, `PolicyEngine`, checkpoint, `ApprovalService`, `ApprovalGrant`, unique execution claim, audit/trace | Dangerous action waits first; a public resume token is not approval; denial has no execution; uncertain executing claims require human review |

## Approval example

The shell command below is allowlisted by the profile, but the `shell.execute` policy still moves the run to waiting:

```bash
agent-harness run examples.dev_assistant \
  --profile local \
  --profiles-dir ./configs/profiles \
  --agents-dir ./agents \
  --prompt 'shell echo reviewed'

agent-harness approvals list <run_id> \
  --profile local \
  --profiles-dir ./configs/profiles

agent-harness approvals approve <approval_id> \
  --profile local \
  --profiles-dir ./configs/profiles \
  --agents-dir ./agents
```

Approval means “execution is allowed”; it does not guarantee success. A deterministic handler failure leaves the run failed while approval remains approved. If a claim entered executing without a result, public approval remains waiting and the system does not replay the external side effect automatically.

## Add your own Agent

From the service-app root:

```bash
agent-harness scaffold agent support.triage
```

The command creates:

```text
agents/support/triage/
├── __init__.py
├── agent.py
├── tools.py
├── schemas.py
├── config.yaml
└── evals/
    ├── drafts/example.yaml
    └── approved/
```

The default configuration uses the fake model, safe budgets, empty `tool_allowlist`, and empty `delegation_edges`. It writes no provider secret and does not move the example draft into `approved/`. There is no `--force`: an existing target, invalid ID, parent-path symlink escape, or failed pre-publication validation exits nonzero without merging or overwriting existing files.

First validate registry discovery and the real executor:

```bash
agent-harness agents list \
  --profile local \
  --profiles-dir ./configs/profiles \
  --agents-dir ./agents

agent-harness run support.triage \
  --profile local \
  --profiles-dir ./configs/profiles \
  --agents-dir ./agents \
  --prompt 'validate scaffold runtime'
```

`evals/drafts/example.yaml` is a human-review seed only. Create a draft from real trace evidence, inspect input/output/expected, and let an explicit reviewer approve it:

```bash
agent-harness eval draft support.triage \
  --dataset-dir ./agents/support/triage/evals \
  --profile local \
  --profiles-dir ./configs/profiles \
  --trigger manual \
  --prompt 'validated input' \
  --output 'observed output' \
  --expected 'confirmed expectation'

agent-harness eval approve <case_id> \
  --dataset-dir ./agents/support/triage/evals \
  --profile local \
  --profiles-dir ./configs/profiles \
  --reviewer <reviewer_id> \
  --reason 'human-reviewed input, expectation, and safety boundary'

agent-harness eval run \
  --dataset-dir ./agents/support/triage/evals \
  --agent-id support.triage
```

The CLI file-dataset runner reads `approved/` only. To execute the Agent for every eval, the application injects a controlled approved-case executor into `EvalRunner.run_file_dataset`; scaffold never auto-approves or executes a draft. Before extending `tools.py` or `tool_allowlist`, add policy, workspace, approval, audit, and corresponding contract tests.
