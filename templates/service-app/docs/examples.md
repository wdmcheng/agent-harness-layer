# 四个 P0 示例 Agent

四个示例都使用 `AgentRegistry -> RunOrchestrator -> AgentExecutor` 公开链路；local profile 使用 fake model、SQLite、local JSONL，不需要真实 API key。它们是扩展点样例，不是完整产品，也不实现 eval experiment、harness comparison 或自动优化。

## 运行与 Eval

```bash
make run-rag
make run-ticket
make run-repo
make run-dev
make eval
```

也可用 `make eval-rag`、`make eval-ticket`、`make eval-repo`、`make eval-dev` 分别运行 approved dataset。所有自动信号仍只能写 drafts；approved JSON 必须经过人工审核，`EvalRunner` 只计 approved case。

## 能力与边界

| Agent | 真实验证链 | 安全降级 |
|---|---|---|
| `examples.rag_assistant` | query 先经过 `EmbeddingInvocationService` 留下 usage evidence，再执行 `RetrievalProvider -> ContextFragment -> ContextAssemblyService -> ModelInvocationService`，返回 citation 和 assembly trace | SQLite FTS5/BM25；无命中返回 `no_source`；retrieval chunk 始终为 `untrusted` |
| `examples.ticket_triage` | typed schema、确定性分类规则、fake model evidence | 低 confidence 返回 `unknown`、`needs_review=true`，不伪造分类 |
| `examples.repo_analyst` | 仅 allowlisted file read/search/list，经 `WorkspacePolicy` 与 artifact store | 越界或 `.agentignore` 命中返回 `tool.workspace_denied`；长结果只内联摘要并保留 `artifact_ref`；shell 不可见 |
| `examples.dev_assistant` | file/shell `ToolRegistry`、PolicyEngine、checkpoint、ApprovalService、ApprovalGrant、唯一 execution claim、audit/trace | 危险动作先 waiting；公开 resume token 不能代替审批；deny 不执行；不确定 executing claim 进入人工复核 |

## Approval 示例

下面的 shell 只允许 profile allowlist 中的命令，但仍会因 `shell.execute` 策略进入 waiting：

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

批准表示“允许执行”，不保证动作成功。handler 返回确定性失败时，run 为 failed、approval 仍为 approved；claim 已进入 executing 却没有 result 时，public approval 保持 waiting，系统不会自动重放外部副作用。

## 新增自己的 Agent

在 service-app 根目录执行：

```bash
agent-harness scaffold agent support.triage
```

命令会生成 `agents/support/triage/`：

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

默认 config 使用 fake model、安全预算、空 `tool_allowlist` 和空 `delegation_edges`；不会写 provider secret，也不会把示例 draft 自动放进 `approved/`。命令没有 `--force`，目标已存在、ID 非法、父路径 symlink 逃逸或发布前验证失败时都会非零退出，且不合并或覆盖已有文件。

先验证 registry 与真实 executor：

```bash
agent-harness agents list \
  --profile local \
  --profiles-dir ./configs/profiles \
  --agents-dir ./agents

agent-harness run support.triage \
  --profile local \
  --profiles-dir ./configs/profiles \
  --agents-dir ./agents \
  --prompt '验证 scaffold runtime'
```

`evals/drafts/example.yaml` 只是人工评审种子。根据真实 trace 创建 draft、检查 input/output/expected 后，再由明确 reviewer 批准：

```bash
agent-harness eval draft support.triage \
  --dataset-dir ./agents/support/triage/evals \
  --profile local \
  --profiles-dir ./configs/profiles \
  --trigger manual \
  --prompt '已验证的输入' \
  --output '已观察的输出' \
  --expected '已确认的期望'

agent-harness eval approve <case_id> \
  --dataset-dir ./agents/support/triage/evals \
  --profile local \
  --profiles-dir ./configs/profiles \
  --reviewer <reviewer_id> \
  --reason '人工确认输入、期望和安全边界'

agent-harness eval run \
  --dataset-dir ./agents/support/triage/evals \
  --agent-id support.triage
```

CLI 的 file dataset runner 只读取 `approved/`；需要每次 eval 都重新执行 Agent 时，由应用在 `EvalRunner.run_file_dataset` 注入受控 approved-case executor，不能让 scaffold 自动批准或执行 draft。扩展 `tools.py` 或 `tool_allowlist` 前，应先补 policy、workspace、approval、audit 和对应 contract tests。
