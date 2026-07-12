# agent-registry-model-context Specification

## Purpose
定义 Agent Registry、模型路由、ContextAssembler 与 Embedding provider/cache 的长期契约，使多 agent 发现、provider 边界、上下文组装和 embedding 复用有稳定 public seam。

## Requirements

### Requirement: AgentRegistry 加载并校验多个 agent descriptor
系统 SHALL 从受控 agent config 目录加载多个 `AgentDescriptor`，并拒绝重复 `agent_id`、无效 schema 或缺少必要字段的配置。每个 `config.yaml` MUST 声明 `agent_id`、`version`、`name`、`description`、`input_schema`、`output_schema`、`model.provider`、`model.default_model`、`model.fallback_models`、`budget.max_tokens_per_run`、`budget.max_cost_usd_per_run`、`tool_allowlist`、`eval_dataset` 和 `delegation_edges`。public descriptor SHALL 只暴露 `agent_id`、`version`、`name`、`description`、schema refs、相对 `config_ref`、tool policy summary、model policy summary、budget summary、eval dataset ref 和 delegation target ids；MUST NOT 暴露本地绝对路径、provider secret、callable 或 provider client。

#### Scenario: 列出已配置 agent
- **WHEN** 调用方通过 CLI 或 API 请求 agent 列表
- **THEN** 系统返回已配置 agent 的 public descriptor 字段，且不暴露本地路径、provider secret 或内部对象

#### Scenario: Descriptor 字段契约完整
- **WHEN** registry 加载 smoke agent config
- **THEN** descriptor 包含 `agent_id`、`version`、输入/输出 schema refs、相对 `config_ref`、模型策略、预算、工具白名单摘要、eval dataset 和 delegation edge 列表

#### Scenario: 重复 agent_id 被拒绝
- **WHEN** registry 加载到两个相同 `agent_id` 的配置
- **THEN** registry 失败并返回稳定错误码，错误详情包含冲突的 `agent_id`

#### Scenario: 无效 agent config 被拒绝
- **WHEN** agent config 缺少必要字段或字段类型不合法
- **THEN** registry 失败并返回 registry validation error，不创建部分可用的脏 registry

### Requirement: Agent executor reference 受控加载且不公开
每个 agent config SHALL 显式声明相对 Python module/callable executor reference；`AgentRegistry` MUST 只解析位于该 config 所属 agent package 内、实现 `AgentExecutor` protocol 的入口，并 MUST NOT 在 public `AgentDescriptor`、API response、CLI list 或序列化 payload 中暴露 callable、module object 或本机绝对路径。Executor contract 生效时 MUST 同步迁移现有 basic/fake agent 与测试 fixture；缺少 executor 的 config MUST 形成结构化 validation error，不得隐式回退到固定 `fake-ok`。

#### Scenario: 合法 executor 被内部 resolver 加载
- **WHEN** registry 加载一个 executor reference 指向该 agent package 内的 callable
- **THEN** internal resolver 返回符合 `AgentExecutor` protocol 的执行入口，public descriptor 字段保持不变

#### Scenario: 越界或无效 executor 整体拒绝 registry
- **WHEN** executor reference 使用绝对路径、越过所属 agent package、引用缺失 module/callable 或对象不符合 protocol
- **THEN** registry 返回结构化 validation error，不加载部分可运行 registry，也不执行引用目标

#### Scenario: 缺少 executor 不走 legacy fallback
- **WHEN** registry 加载现有或新增的 agent config 而该 config 没有显式 executor reference
- **THEN** registry 返回结构化 validation error，不注册该 agent，也不通过 `RunOrchestrator` 生成固定 `fake-ok` output

### Requirement: Agent list API 和 CLI 使用同一 registry seam
系统 SHALL 提供 `agent-harness agents list` 和 `GET /api/v1/agents`，二者都通过同一 `AgentRegistry` 读取 descriptor，并使用稳定 DTO / `ApiErrorEnvelope`。

#### Scenario: CLI agents list 离线可用
- **WHEN** developer 在 local profile 下执行 `agent-harness agents list`
- **THEN** command 输出至少一个 registry smoke agent，且不需要真实模型 API key

#### Scenario: OpenAPI 包含 agents list 契约
- **WHEN** 生成 service-app OpenAPI schema
- **THEN** `/api/v1/agents` 存在 `GET` operation，成功响应使用 agent list schema，错误响应包含 `ApiErrorEnvelope`

#### Scenario: Registry validation error 映射到 API error envelope
- **WHEN** service app 启动或测试入口注入无效 registry
- **THEN** `/api/v1/agents` 返回稳定错误 envelope，而不是泄露 Pydantic、YAML 或本地路径异常

#### Scenario: Unknown agent run 被 registry 拦截
- **WHEN** 调用方通过 CLI 或 `POST /api/v1/agents/{agent_id}/runs` 请求不存在的 `agent_id`
- **THEN** 系统在进入 `RunOrchestrator` 前通过 `AgentRegistry` 拒绝请求，并返回 `registry.agent_not_found`

### Requirement: Delegation edge 与摘要接缝默认受控
系统 SHALL 从 agent descriptor 读取 delegation edge，并提供显式校验 seam；未声明 edge 时默认拒绝 agent 互调。当前接缝只校验 edge，并可把调用方提供的 parent/child run、usage、budget 与 trace refs 组装为 `DelegationSummary`；它 MUST NOT 被解释为已经创建 child run、调用 target executor、持久化聚合或完成跨 agent 调度。

#### Scenario: 未声明 delegation edge 被拒绝
- **WHEN** agent A 请求委派给 agent B 且 A 的 descriptor 未声明 B
- **THEN** registry delegation check 返回拒绝结果，并保留 source agent、target agent 和 reason

#### Scenario: 已声明 delegation edge 允许继续
- **WHEN** agent A 请求委派给 agent B 且 A 的 descriptor 已声明 B
- **THEN** registry delegation check 返回允许结果；调用方可另行请求组装 delegation summary，但 registry 不创建或执行 child run

#### Scenario: 调用方提供的摘要字段按原样收口
- **WHEN** 调用方在已声明 edge 上提供 parent run、delegated run、usage、budget 和 trace refs
- **THEN** registry 返回 provider-neutral `DelegationSummary`，但不声称这些 refs 已由 child execution 验证、持久化或归并到 parent run

### Requirement: ModelRouter 通过 provider-neutral 接缝执行路由和预算判断
系统 SHALL 通过 `ModelProvider` interface 和 `ModelRouter` 选择默认/任务级模型，执行 timeout、fallback 和预算估算，并为 provider、budget 配置变更提供显式 reload/restart seam。

#### Scenario: Fake model 不需要真实 API key
- **WHEN** local profile 使用 fake provider 运行 tests 或 smoke
- **THEN** 模型调用成功返回可预测结果，且不读取真实 provider key

#### Scenario: 预算超阈值产生可追踪 fallback decision
- **WHEN** 模型调用预计 token 或 cost 超过配置阈值
- **THEN** `ModelRouter` 返回可追踪的 fallback / policy-needed decision summary，包含估算值、阈值和选定动作

#### Scenario: 业务 agent 不直接 import Pydantic AI
- **WHEN** import boundary check 扫描 runtime core、template app 和业务 agent
- **THEN** `pydantic_ai` import 只允许出现在 `agent_harness.adapters.models.pydantic_ai` 或测试中的受控 adapter seam

### Requirement: ContextAssembler 输出可解释 assembly trace
系统 SHALL 通过 `ContextAssembler` 收口 system/user/history/retrieval/tool output/artifact refs，按 token budget 执行裁剪和降级，并输出包含 source、trust_level、truncation 和 fallback decision 的 trace。每次 assembly SHALL 写入 `context_assemblies` 记录，包含 input refs、token budget、trust summary、truncation summary 和 output_ref。

#### Scenario: 多来源上下文被统一组装
- **WHEN** 历史、retrieval chunk、tool output 和 artifact ref 同时进入上下文
- **THEN** assembly 输出保留每个片段的 source_ref、trust_level、token estimate 和截断状态

#### Scenario: 超预算时按可解释顺序降级
- **WHEN** 输入上下文超过 token budget
- **THEN** assembler 先裁剪 history，再截断 retrieval/tool output，必要时记录 fallback model 或 policy-needed decision

#### Scenario: Context assembly 记录可持久化读取
- **WHEN** assembler 完成一次上下文组装
- **THEN** storage repository 可按 assembly id 读取 input refs、token budget、trust summary、truncation summary 和 output_ref

### Requirement: EmbeddingProvider 支持 mock/local、OpenAI-compatible adapter 和 cache
系统 SHALL 通过 `EmbeddingProvider` interface 生成 embedding，local tests 默认使用 mock/local provider，并通过 `embedding_cache` 持久化记录复用重复输入结果。cache key SHALL 包含 provider、model 和 input hash，cache metadata SHALL 记录 hit/miss、vector_ref 和 provider latency。

#### Scenario: 重复 embedding 输入命中 cache
- **WHEN** 同一 provider、model 和 input hash 第二次请求 embedding
- **THEN** cache 返回已有 vector ref 或 embedding result，并记录 cache hit metadata

#### Scenario: Embedding cache 记录可跨 repository instance 复用
- **WHEN** 同一 SQLite 或 PostgreSQL storage 中重新构造 embedding cache repository
- **THEN** 第二次请求同一 provider、model 和 input hash 仍命中已有 cache record

#### Scenario: OpenAI-compatible adapter 不污染业务边界
- **WHEN** 配置 OpenAI-compatible embedding provider
- **THEN** provider SDK / HTTP 细节只存在于 adapter 层，业务 agent 和 context assembler 只依赖 `EmbeddingProvider`
