# 扩展指南

[English](extension-guide.md) | [简体中文](extension-guide.zh-CN.md)

适用读者：在 `templates/service-app` 增加业务能力的 app developer，以及维护公共 seam 的 scaffold maintainer。

导航：[根 README](../README.zh-CN.md) · [五层两翼开发 Agent](building-an-agent.zh-CN.md) · [架构边界](architecture/README.zh-CN.md) · [Adapter 合同](adapter-contracts.zh-CN.md) · [Context 与信任边界](context-and-trust-boundary.zh-CN.md) · [安全策略](security-policy.zh-CN.md) · [Eval/Observability](eval-observability-loop.zh-CN.md)

## 扩展原则

先选择已有公开 seam，再新增实现。`agents/*` 不直接 import vendor SDK；`app/*` 只做协议入口、依赖装配和响应转换；核心 `agent_harness/*` 不依赖 template 或具体 agent。外部 SDK 只允许进入 `agent_harness/adapters/*` 或经 import boundary 明确批准的 integration 模块。跨边界只传 Pydantic DTO、`CanonicalEvent`、protocol、facade、repository 或 UoW，不传 ORM session、SDK object 和进程内可变全局。

`make quality` 会执行 `scripts/import_boundary_check.py`。绕过检查、把 SDK 放进业务 agent，或者为了过检查弱化规则，都不是扩展方式。

下文的 direct local CLI 命令都假设已经按 [`templates/service-app` 首次使用](../templates/service-app/README.zh-CN.md#首次使用local-profile) 导出 `AGENT_HARNESS_BUDGET__FINGERPRINT_KEY`/storage DSN 并完成 SQLite migration。缺少任一前置时应 fail closed，不能把 `config.invalid` 或 `storage.migration_required` 当成工具故障。

## Agent

- 公开 seam：`AgentDescriptor`、`AgentRegistry`、`AgentExecutor`，以及每个 agent 的 `config.yaml`、`agent.py`、`schemas.py`。
- 操作：从复制后的 service-app 根目录运行 `uv run agent-harness scaffold agent support.triage`；补 executor 和 schema；审核生成的 draft eval case；用 registry 列表确认加载。
- 禁止：在 `app/*` 写业务 agent；从 agent 读取 profile YAML；绕过 registry/policy 发起 delegation；默认赋予工具权限。
- 验证：`uv run agent-harness agents list --profile local --profiles-dir ./configs/profiles --agents-dir ./agents`，随后运行该 agent 和 approved eval。
- 证据：`tests/contracts/test_agent_scaffold_cli_contracts.py`、`tests/contracts/test_agent_registry_router_model_contracts.py`、`templates/service-app/docs/examples.zh-CN.md`。
- 排障：列表缺失时先检查点分小写 `agent_id`、配置 schema 和 executor import；scaffold 拒绝目标时检查 symlink、已存在目录和 root discovery，不要用 `--force`，该参数不存在。

## Tool 与 MCP

- 公开 seam：`ToolRegistry`、工具 descriptor/结果 DTO、`WorkspacePolicy`、`PolicyEngine`；MCP 通过 `MCPClient` 和 `MCPTool` 适配。`ApprovedToolExecutor` 是 registry 内部的审批执行实现，不是 `agent_harness.tools` 的公开导出，调用方不得直接依赖。
- 操作：注册最小 schema 和权限；把 workspace root、路径规则和 environment allowlist 明确写入配置；危险调用必须由 policy 返回 allow/deny/require-approval。
- 禁止：直接执行未经 registry 的 callable；把宿主环境完整传给 shell；让路径逃逸 workspace；在 approval 前产生外部副作用；把 MCP raw response 透传进公共事件。
- 验证：完成 local 初始化后，从 service-app 根目录运行 `uv run agent-harness policy check --profile local --profiles-dir ./configs/profiles --storage-dsn "$STORAGE_DSN" --action run.read --resource run`，再执行相关 agent run 与 `make test`。
- 证据：`tests/contracts/test_tool_registry_public_seam_contracts.py`、`tests/contracts/test_tool_registry_authorization_contracts.py`、`tests/contracts/test_approval_execution_contracts.py`。
- 排障：工具不可见时先看 agent allowlist 与 identity permission；返回 409 时检查是否需要 approval；路径拒绝时检查规范化后的 workspace 相对路径，不要扩大 root。

## Model 与 embedding

- 公开 seam：`ModelProvider`、`ModelStructuredProvider`、`ModelRequest`、`ModelResponse`、`StructuredOutputResult`、`ModelRouter`、`ModelInvocationService`；embedding 使用 `EmbeddingProvider` 与 cache protocol。
- 操作：在 `agent_harness/adapters/models/` 实现 provider；在 composition root 绑定；保持 fake provider 只用于显式 local/test route；为 usage/cost/latency 和重放补合同证据。业务 Agent 需要结构化结果时，在 `config.yaml` 绑定严格 `output_schema`，再从 bound execution 调用 `complete_structured(..., operation_key=..., repair_limit=0..2)`；调用方只能缩小 repair 上限，不能传入另一份 schema。
- 禁止：业务 agent import Pydantic AI 或 provider SDK；把 provider/Pydantic object 放进 DTO；在调用前跳过 identity、budget、policy 或 approval；用虚构的零成本替代 unavailable；在 adapter 内 repair/retry、把无效候选原文写入 evidence、把 structured 调用退回普通文本/fake，或把结构化 JSON 解释为工具调用。
- 验证：`make test`、`make smoke-local`，涉及真实跨进程持久化再跑 `make smoke-service`。
- 证据：`tests/contracts/test_agent_registry_router_model_contracts.py`、`tests/contracts/test_model_usage_invocation_contracts.py`、`tests/contracts/test_model_usage_runtime_composition_contracts.py`、`tests/contracts/test_provider_neutral_structured_public_seam_contracts.py`。
- 排障：route 失败先查 profile、model policy、deployment capability 与 Agent `output_schema_identity`；重复 usage 检查稳定 call id、operation/replay identity、outbox 和 settlement；`model.structured_schema_unknown`先修 Registry 全量加载，`needs_review`保留 reservation 并人工核对，不重发 provider。Provider 失败只保留结构化脱敏错误，不记录 raw response/无效候选。

## Retrieval

- 公开 seam：`RetrievalProvider`、`RetrievalResult`、`ContextFragment`；当前实现包含 local SQLite BM25、PostgreSQL native FTS，以及可选 PGroonga/pgvector adapter。
- 操作：实现 provider protocol，把结果经 `retrieval_result_to_context_fragment` 转为带 `source_ref`/`trust_level` 的 context，再由 `ContextAssembler` 做预算与 trace。
- 禁止：把检索结果直接拼进 prompt 而丢失来源/信任；让 optional extension 成为 local profile 硬依赖；跨 tenant 读取索引。
- 验证：完成上述 local 初始化后运行 `uv run agent-harness doctor --profile local --profiles-dir ./configs/profiles --storage-dsn "$STORAGE_DSN"`、RAG 示例和 `make test`；PostgreSQL 路径使用 `make smoke-service`。
- 证据：`tests/contracts/test_retrieval_rag_contracts.py`、`tests/contracts/test_retrieval_doctor_example_contracts.py`。
- 排障：extension 不可用时确认 capability probe 和 native FTS 降级；结果为空时检查 tenant、metadata 和 index，不要用无来源文本掩盖失败。

## Observability

- 公开 seam：`TelemetryFacade`、`ProviderTelemetryAdapter`、`TelemetryRecord`、`CanonicalEvent` 和 OTel mapping。
- 操作：本地 evidence 先提交，再 fan-out 可选 Logfire/Phoenix/Langfuse adapter；新 provider 实现 protocol、脱敏和 degradation 状态，不改变业务调用方。
- 禁止：业务代码直接 import provider SDK；provider 失败回滚本地 event；把 secret、绝对路径或超大 raw payload 发给 provider。
- 验证：`make smoke-local`、`make test`；provider 配置/降级合同见下列证据。
- 证据：`tests/contracts/test_observability_local_first_fanout_contracts.py`、`tests/contracts/test_observability_provider_adapters_contracts.py`、`tests/contracts/test_observability_provider_configuration_contracts.py`。
- 排障：provider degraded 时先看本地 `CanonicalEvent`/JSONL 是否完整，再看脱敏状态摘要；不要把 SaaS 不可用误报成 run 失败。

## Eval

- 公开 seam：approved case repository、`ApprovedCaseExecutor`、`ExperimentEvaluator`、`ExperimentEvidencePublisher`、score sink 与 acceptance service。
- 操作：detector 只写 draft；人工 review 后才能进入 approved；experiment 固定 split/manifest 后比较 baseline/candidate，最后由 reviewer、policy 和 audit 共同决定 acceptance。
- 禁止：自动写 approved；让总分提升覆盖 holdout/critical regression；由 evaluator 直接改生产 prompt/config；把 provider failure 当 local evidence 成功。
- 验证：`make eval`、`make test`；HTTP/CLI experiment 操作见 [Eval/Observability 闭环](eval-observability-loop.zh-CN.md)。
- 证据：`tests/contracts/test_eval_gate_trace_loop_contracts.py`、`tests/contracts/test_eval_execution_contracts.py`、`tests/contracts/test_eval_experiment_api_contracts.py`。
- 排障：`no-approved-cases` 是稳定结果，不是伪造通过；`needs_review` 要人工核对 claim/evaluator evidence，不能强制重跑。

## 完成前检查

```bash
make quality
make test
make eval
make smoke-local
# 只有扩展影响 PostgreSQL、Redis、DBOS、API/worker 协作时才有资格用它证明 service：
make smoke-service
```

变更若改变行为，先更新 Product Spec/DEV Plan 或对应 OpenSpec change。若改变 API，再先更新 `API-Contract.md`。新增 vendor 或 runtime 还必须复核 [Adapter 合同](adapter-contracts.zh-CN.md)、[ADR-0002](adr/0002-vendor-adapter-isolation.zh-CN.md) 与 [release process](release-process.zh-CN.md)。
