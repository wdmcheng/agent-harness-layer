## ADDED Requirements

### Requirement: Model 与 embedding 产生统一 provider-neutral usage evidence
系统 SHALL 为每次 model 或 embedding 调用产生不可由业务 agent 手工构造的 `ModelUsageEvidence`。Evidence MUST 严格使用 API Contract 5.29 的字段：`usage_kind=model|embedding`、`tenant_id`、provider、model、nullable `input_tokens`/`output_tokens`、nullable `cost_usd`、`cost_status`、`latency_ms`、decision、`run_id`、`agent_id`、可选 `request_id` 和必填 `trace_id`；不适用或不可得字段 MUST 保持 null/`unavailable`，不得新增另一套同义 public DTO 字段或伪造关联/数值。

#### Scenario: Fake model 完成调用
- **WHEN** local fake provider 完成一次 model 调用
- **THEN** 产生 `usage_kind=model` 的统一 evidence，provider/model、token、latency、route/budget decision 与 tenant/run/agent/trace 关联均可从稳定字段读取，缺失 request correlation 时 `request_id` 为 null

#### Scenario: Embedding adapter 完成调用
- **WHEN** local 或 OpenAI-compatible embedding adapter 替身完成一次调用
- **THEN** 产生相同 DTO 形状的 `usage_kind=embedding` evidence，且不包含 embedding 原文、vector 全文或 provider SDK 对象

### Requirement: Cost 与 token 可用性不伪造
Provider 报告的 USD cost SHALL 写入 `cost_usd` 并标记 `reported`；仅当存在可验证、带来源或版本的 price configuration 时才可计算并标记 `estimated`。Estimated evidence MUST 在既有 `decision` 对象中写入安全的 `price_source_ref` 与 `price_source_version`，不得新增顶层同义字段或内联完整价目。Provider 未报告且无可验证价格时，`cost_usd` MUST 为 null 且 `cost_status=unavailable`，不得写 0。Token 不可用 MUST 为 null，与真实零 token 分开。

#### Scenario: Provider 报告 cost
- **WHEN** adapter 收到 provider 可验证的 token 与 cost 数据
- **THEN** evidence 保留规范化 `cost_usd` 和 `cost_status=reported`，不携带 raw response

#### Scenario: 价格不可验证
- **WHEN** provider 未返回 cost 且当前配置没有可验证价目来源
- **THEN** evidence 的 `cost_usd` 为 null、`cost_status=unavailable`，不以 0 暗示免费调用

#### Scenario: 可验证配置产生估算
- **WHEN** provider 未返回 cost，但受控 price configuration 可由 provider/model/token 确定估算值
- **THEN** evidence 标记 `estimated`，在 `decision.price_source_ref` 与 `decision.price_source_version` 保留安全来源，不新增顶层字段且不把估算伪装为 provider 报告值

### Requirement: 调用生命周期和失败 evidence 可关联
系统 SHALL 在 provider 副作用前生成稳定 `usage_call_id` 并发布 `model.request.started` 或等价 embedding started event，在完成、受控拒绝或 provider 失败后发布恰好一条 terminal usage event。Started 与 terminal event MUST 逐值共享 tenant/run/request/agent/trace、provider/model 和 `usage_call_id` correlation；`usage_call_id` 只属于 CanonicalEvent/telemetry metadata，不扩张 `ModelUsageEvidence` public 字段。失败 event MUST 保留已知 latency、usage 和 route/budget decision，并通过 CanonicalEvent envelope 使用稳定、脱敏 error code/summary。

#### Scenario: 完成路径恰好一次结算
- **WHEN** provider 调用成功完成
- **THEN** 同一调用只有一条 terminal usage evidence，并可由 started correlation 找到，不因 telemetry fan-out 重复结算

#### Scenario: Budget 或 policy 在调用前拒绝
- **WHEN** route/budget decision 要求 fallback、policy intervention 或拒绝且 provider 尚未调用
- **THEN** terminal evidence 记录 decision、outcome 和零 provider side effect，不伪造 provider token/cost

#### Scenario: Provider 失败仍留下安全 evidence
- **WHEN** provider timeout 或异常包含 secret、prompt 片段或 raw response 内容
- **THEN** terminal evidence 保留关联、latency、已知 usage 和稳定 error code，但 DTO、event、trace、error 和 provider payload 均不包含原始敏感内容

### Requirement: Local fake run 满足入口时延门禁
local profile SHALL 使用无网络依赖的 fake provider，从公开单 agent run 入口到唯一 terminal 记录 monotonic 总时延并执行小于 5 秒的稳定门禁。验证 evidence MUST 输出可定位的阶段时延和 run/trace 关联，单元测试内部微步骤墙钟不得替代该入口证据。

#### Scenario: Local fake run 在阈值内完成
- **WHEN** 固定 local fixture 从公开入口创建并完成 single-agent fake run
- **THEN** smoke 读取同一 run 的 terminal 与 usage evidence，总时延小于 5 秒且无真实 API key 或外部网络依赖

#### Scenario: 时延超限可定位
- **WHEN** 入口到 terminal 总时延达到或超过 5 秒
- **THEN** smoke 非零失败并输出不含 secret 的阶段时延与关联标识，不以跳过、放宽阈值或单元测试结果替代
