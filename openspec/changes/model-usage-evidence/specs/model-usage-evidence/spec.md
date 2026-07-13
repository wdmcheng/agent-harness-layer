## ADDED Requirements

### Requirement: Model 与 embedding 产生统一 provider-neutral usage evidence
系统 SHALL 为每次 model 或 embedding 调用产生不可由业务 agent 手工构造的 `ModelUsageEvidence`。Evidence MUST 严格使用 API Contract 5.29 的字段：`usage_kind=model|embedding`、`tenant_id`、provider、model、nullable `input_tokens`/`output_tokens`、nullable `cost_usd`、`cost_status`、`latency_ms`、decision、`run_id`、`agent_id`、可选 `request_id` 和必填 `trace_id`；token 与 latency 必须是非 bool 的非负整数，cost 必须是非 bool、有限且非负的 number。bool、负值、NaN、正负 Infinity 或不适用/不可得字段 MUST 在持久化与聚合前拒绝或保持 null/`unavailable`，不得新增另一套同义 public DTO 字段、伪造关联/数值或让负值反向冲减预算。

#### Scenario: Fake model 完成调用
- **WHEN** local fake provider 完成一次 model 调用
- **THEN** 产生 `usage_kind=model` 的统一 evidence，provider/model、token、latency、route/budget decision 与 tenant/run/agent/trace 关联均可从稳定字段读取，缺失 request correlation 时 `request_id` 为 null

#### Scenario: Embedding adapter 完成调用
- **WHEN** local 或 OpenAI-compatible embedding adapter 替身完成一次调用
- **THEN** 产生相同 DTO 形状的 `usage_kind=embedding` evidence，且不包含 embedding 原文、vector 全文或 provider SDK 对象

#### Scenario: Embedding cache hit 仍产生调用级 evidence
- **WHEN** embedding adapter 命中 tenant-scoped cache 且没有调用 provider
- **THEN** 系统仍发布一组 started/final evidence，`latency_ms` 是本次 cache lookup 墙钟，token/cost 为 null、`cost_status=unavailable`，decision 明确 `cache_status=hit` 与 `provider_called=false`；cache row 的首次 `provider_latency_ms` 不得成为本次 latency，provider side-effect count 为零

### Requirement: Cost 与 token 可用性不伪造
Provider 报告的非负有限 USD cost SHALL 写入 `cost_usd` 并标记 `reported`；仅当存在可验证、带来源或版本的 price configuration 时才可计算非负有限 cost 并标记 `estimated`。`reported|estimated` MUST 与非 null `cost_usd` 同时出现；`unavailable` MUST 与 `cost_usd=null` 同时出现。Estimated evidence MUST 在既有 `decision` 对象中写入安全的 `price_source_ref` 与 `price_source_version`，不得新增顶层同义字段或内联完整价目。Provider 未报告且无可验证价格时不得写 0。Token 不可用 MUST 为 null，与真实零 token 分开。

#### Scenario: Provider 报告 cost
- **WHEN** adapter 收到 provider 可验证的 token 与 cost 数据
- **THEN** evidence 保留规范化 `cost_usd` 和 `cost_status=reported`，不携带 raw response

#### Scenario: 价格不可验证
- **WHEN** provider 未返回 cost 且当前配置没有可验证价目来源
- **THEN** evidence 的 `cost_usd` 为 null、`cost_status=unavailable`，不以 0 暗示免费调用

#### Scenario: 可验证配置产生估算
- **WHEN** provider 未返回 cost，但受控 price configuration 可由 provider/model/token 确定估算值
- **THEN** evidence 标记 `estimated`，在 `decision.price_source_ref` 与 `decision.price_source_version` 保留安全来源，不新增顶层字段且不把估算伪装为 provider 报告值

#### Scenario: 非法数值与 cost 状态组合在聚合前被拒绝
- **WHEN** adapter 或持久化历史 evidence 提供 bool、负 token/cost/latency、NaN/Infinity、`reported|estimated` 配 null cost，或 `unavailable` 配非 null cost
- **THEN** DTO/repository/EventBus 在持久化或 delegation 预算聚合前以稳定 validation/state error fail closed，不发布可信 usage、不更新预算且不回显 provider raw value

### Requirement: 调用生命周期和失败 evidence 可关联
系统 SHALL 在 provider 副作用前生成稳定 `usage_call_id` 并发布 `model.request.started`，在完成、受控拒绝或 provider 失败后发布恰好一条调用级最终 `model.usage.updated`。Model 与 embedding 精确复用这两个 event type，并以 `ModelUsageEvidence.usage_kind` 区分，不得新增等价 embedding event。Started 与最终 usage event MUST 逐值共享 tenant/run/request/agent/trace、provider/model 和 `usage_call_id` correlation；`model.usage.updated` 只结束该 `usage_call_id`，其 `CanonicalEvent.terminal` MUST 为 false，run terminal marker 仍只允许 `run.completed`、`run.failed`、`run.cancelled`。`usage_call_id` 只属于 CanonicalEvent/telemetry metadata，不扩张 `ModelUsageEvidence` public 字段。失败 event MUST 保留已知 latency、usage 和 route/budget decision，并通过 CanonicalEvent envelope 使用稳定、脱敏 error code/summary。

每次 started 调用 MUST 在 provider 副作用前建立以 `(tenant_id, usage_call_id)` 唯一的 durable settlement/outbox 与稳定 usage event id；provider 结果、脱敏 usage 摘要或确定性失败 MUST 只写入该状态一次。sink 写入失败、确认丢失或进程重启后，恢复 MUST 从已持久化结果幂等补投同一 event id，MUST NOT 重新调用 provider。每条 run-scoped `model.usage.updated` 的 `seq` MUST 小于同一 run 的 terminal event `seq`；runtime 发布 terminal 前 MUST 恢复或确定性封闭所有已开始的 usage 调用，未知结果 MUST 保持 pending/needs_review并阻止 terminal，EventBus/sink MUST 拒绝 terminal 后的任何后续业务事件。

#### Scenario: 完成路径恰好一次结算
- **WHEN** provider 调用成功完成
- **THEN** 同一调用只有一条最终 usage evidence，并可由 started correlation 找到，不因 telemetry fan-out 重复结算，且不会以 run terminal marker 提前关闭事件流

#### Scenario: Fallback 调用实际备用 provider
- **WHEN** router 在 provider 调用前选择 fallback route
- **THEN** 系统只调用选定的备用 provider，调用级最终 usage evidence 记录原 route decision 与实际 provider/model，且 `CanonicalEvent.terminal=false`

#### Scenario: Budget 或 policy 在调用前硬拒绝
- **WHEN** hard budget decision、policy intervention 或 policy rejection 阻止 provider 调用
- **THEN** 调用级最终 usage evidence 记录 decision、outcome 和零 provider side effect，不伪造 provider token/cost，且 `CanonicalEvent.terminal=false`

#### Scenario: Provider 失败仍留下安全 evidence
- **WHEN** provider timeout 或异常包含 secret、prompt 片段或 raw response 内容
- **THEN** 最终 usage evidence 保留关联、latency、已知 usage 和稳定 error code，但 DTO、event、trace、error 和 provider payload 均不包含原始敏感内容

#### Scenario: Run terminal 等待调用级结算
- **WHEN** run 准备发布 terminal event 且仍有已开始但未写最终 usage evidence 的 model/embedding 调用
- **THEN** runtime 先等待或确定性封闭每个调用并写唯一最终 usage，随后才写 terminal；terminal 持久化后任何晚到业务事件都被拒绝

#### Scenario: Usage sink 失败由 durable settlement 恢复
- **WHEN** provider 结果已经持久化，但最终 usage sink 在写前失败、写后确认丢失或进程随即退出
- **THEN** recovery 使用同一 `usage_call_id`、稳定 event id和既有脱敏结果幂等补投唯一 usage，provider 调用次数保持一次；补投完成前 run terminal 不可见

### Requirement: Local fake run 满足入口时延门禁
local profile SHALL 使用无网络依赖的 fake provider，从公开单 agent run 入口到唯一 terminal 记录 monotonic 总时延并执行小于 5 秒的稳定门禁。验证 evidence MUST 输出可定位的阶段时延和 run/trace 关联，单元测试内部微步骤墙钟不得替代该入口证据。

#### Scenario: Local fake run 在阈值内完成
- **WHEN** 固定 local fixture 从公开入口创建并完成 single-agent fake run
- **THEN** smoke 分别读取同一 run 的 run terminal 与调用级最终 usage evidence，总时延小于 5 秒且无真实 API key 或外部网络依赖

#### Scenario: 时延超限可定位
- **WHEN** 入口到 terminal 总时延达到或超过 5 秒
- **THEN** smoke 非零失败并输出不含 secret 的阶段时延与关联标识，不以跳过、放宽阈值或单元测试结果替代
