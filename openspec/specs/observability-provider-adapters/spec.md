## Purpose

定义 provider-neutral 的统一观测边界、trace 关联、local-first evidence、外部 provider adapter 隔离和统一脱敏要求。

## Requirements

### Requirement: TelemetryFacade 统一观测输出边界
系统 SHALL 暴露 `TelemetryFacade`，用于接收 CanonicalEvent 或 provider-neutral telemetry record，并统一完成 trace context 合并、payload redaction、local/jsonl evidence 写入和外部 provider fan-out。Facade MUST NOT 向 API、CLI、runtime、template app 或业务 agent 暴露 provider SDK client、transport object、raw span、raw response 或未脱敏异常。

#### Scenario: 未配置 SaaS provider 仍写入 local evidence
- **WHEN** 调用方通过 `TelemetryFacade` 发布 runtime、tool、model、retrieval、eval、approval 或 audit 事件且未配置任何外部 provider
- **THEN** local/jsonl sink 写入脱敏后的 telemetry evidence，并可按 `run_id` / `trace_id` 读取

#### Scenario: provider fan-out 不泄漏 SDK object
- **WHEN** facade 将 telemetry record 发送给外部 provider adapter
- **THEN** adapter 只收到 provider-neutral DTO 和脱敏 payload，不收到 provider SDK object、ORM model、raw exception 或业务对象

### Requirement: Trace context 关联字段稳定传播
系统 SHALL 提供 `TelemetryContext` 或等价 DTO，用于在 runtime、tool、model、retrieval、eval、approval 和 audit 事件之间传播 `trace_id`、`span_id`、`parent_span_id`、`tenant_id`、`user_id`、`agent_id`、`run_id`、`session_id`、`request_id` 以及适用的 `tool_name`、`model_provider`、`model_name`、`eval_run_id`。缺失非适用字段时 MUST 保持字段可选，而不是伪造错误关联。

#### Scenario: runtime 事件带核心关联字段
- **WHEN** runtime 发布 run started/completed/failed telemetry record
- **THEN** record 至少携带 `trace_id`、`tenant_id`、`agent_id`、`run_id` 和适用的 `user_id` / `session_id`

#### Scenario: tool/model/retrieval/eval 字段按适用性追加
- **WHEN** tool、model、retrieval 或 eval 事件发布 telemetry record
- **THEN** record 保留核心 run correlation，并追加对应的 tool/model/retrieval/eval metadata；不适用字段保持缺省

### Requirement: OTel exporter contract 隔离在观测 adapter 层
系统 SHALL 提供 OTel exporter adapter contract，把脱敏后的 telemetry record 映射为 OpenTelemetry span、metric、event 和 attributes。OTel API/SDK/exporter imports MUST 限定在 observability adapter 或受控 integration boundary；core contracts、business agent、template app 和 eval runner MUST NOT 直接 import OTel exporter SDK。

#### Scenario: OTel adapter 输出稳定 span 和 event attributes
- **WHEN** OTel adapter 接收包含 trace context 的 telemetry record
- **THEN** 生成的 span/event attributes 包含稳定 envelope 字段、关联字段和脱敏 payload 摘要，不包含 raw secret 或 provider 原始对象

#### Scenario: OTel adapter 输出稳定 metric attributes
- **WHEN** telemetry record 包含 duration、token usage、cost、count 或其他数值型观测摘要
- **THEN** OTel adapter 生成 provider-neutral metric payload，metric attributes 复用同一批 correlation 字段和脱敏 metadata，不内联完整大 payload

#### Scenario: OTel exporter failure 不影响 local evidence
- **WHEN** OTel exporter 抛出连接、认证或超时错误
- **THEN** facade 保留 local/jsonl evidence，并只记录脱敏 provider failure summary

### Requirement: Logfire/Phoenix/Langfuse adapter contract 不污染业务边界
系统 SHALL 提供 Logfire、Phoenix 和 Langfuse observability adapter contract。Provider SDK imports MUST 只出现在 `agent_harness.adapters.observability` 或明确批准的 integration path；业务 agent、template app、eval runner、core contracts 和 API route MUST NOT 直接 import `logfire`、`phoenix`、`langfuse` 或相关 provider client。

#### Scenario: Provider adapter contract 可用 fake client 验证
- **WHEN** tests 使用 fake Logfire、Phoenix 或 Langfuse client/module 调用 adapter
- **THEN** adapter 将脱敏 telemetry record 转换为 provider payload，并返回 sent/degraded status，不需要真实 API key 或 SaaS 账号

#### Scenario: provider SDK import boundary 被扫描
- **WHEN** vendor boundary tests 扫描 business/template/example surfaces
- **THEN** 直接 import Logfire、Phoenix 或 Langfuse SDK 会失败，adapter/integration path 之外没有 provider SDK import

### Requirement: Provider 前 redaction 阻止 secret 写入所有观测面
系统 SHALL 在 telemetry record 写入 local/jsonl、OTel attributes、Logfire/Phoenix/Langfuse provider payload、audit-like evidence、eval-like payload 或 error summary 前执行统一 redaction。Secret-like key、token、password、cookie、Authorization header、API key、provider raw response 和完整大 payload MUST 被脱敏、替换为 artifact/ref 或阻止写入。

#### Scenario: secret fixture 不进入 local 和 provider payload
- **WHEN** telemetry payload、trace context、error detail 或 metadata 包含 token、password、cookie、Authorization header 或 API key
- **THEN** local/jsonl record 和 provider payload 均不包含原始 secret value，只包含脱敏摘要或 ref

#### Scenario: provider exception 被脱敏
- **WHEN** provider adapter 抛出的异常 message 包含 secret-like 字符串
- **THEN** facade 记录的 provider failure summary 不包含原始 secret value，也不把异常作为 raw payload 写入 event/audit/trace

### Requirement: Observability 配置与 doctor 保持 local-first
系统 SHALL 在 profile config 中支持 local/jsonl、OTel endpoint 和 provider adapter 配置。local profile MUST 默认不需要 SaaS key 或外部 provider；service profile MAY 配置 provider adapter，但 provider 缺凭据或不可达时 doctor MUST 输出可操作状态，且不得把 optional provider 缺失当作 local evidence 不可用。

#### Scenario: local profile 不需要 provider key
- **WHEN** developer 加载 local profile 或运行 `agent-harness doctor --profile local`
- **THEN** observability status 报告 local/jsonl 可写，并在没有 SaaS provider key 时成功

#### Scenario: service profile provider 不可达时降级
- **WHEN** service profile 配置外部 provider 但 provider endpoint、token 或网络不可用
- **THEN** doctor 或 facade 输出脱敏降级提示，local/jsonl evidence 仍可写
