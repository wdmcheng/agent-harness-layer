# model-usage-evidence Specification

## Purpose
TBD - created by archiving change model-usage-evidence. Update Purpose after archive.
## Requirements
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
系统 SHALL 在 provider 副作用前生成稳定 `usage_call_id` 并发布 `model.request.started`，在完成、受控拒绝或 provider 失败后发布恰好一条调用级最终 `model.usage.updated`。Composition MUST 从 durable tenant/run/request/agent/trace 关联与稳定的语义调用槽位生成该 ID；invocation seam MUST NOT 随机回退，也 MUST NOT 接受 prompt、embedding input、secret 或其他敏感业务输入作为调用槽位。Model 与 embedding 精确复用这两个 event type，并以 `ModelUsageEvidence.usage_kind` 区分，不得新增等价 embedding event。Started 与最终 usage event MUST 逐值共享 tenant/run/request/agent/trace、provider/model 和 `usage_call_id` correlation；`model.usage.updated` 只结束该 `usage_call_id`，其 `CanonicalEvent.terminal` MUST 为 false，run terminal marker 仍只允许 `run.completed`、`run.failed`、`run.cancelled`。`usage_call_id` 只属于 CanonicalEvent/telemetry metadata，不扩张 `ModelUsageEvidence` public 字段。失败 event MUST 保留已知 latency、usage 和 route/budget decision，并通过 CanonicalEvent envelope 使用稳定、脱敏 error code/summary。

每次 started 调用 MUST 在 provider 副作用前建立以 `(tenant_id, usage_call_id)` 唯一的 durable settlement/outbox 与稳定 usage event id；provider 结果、脱敏 usage 摘要或确定性失败 MUST 只写入该状态一次。sink 写入失败、确认丢失或进程重启后，恢复 MUST 从已持久化结果幂等补投同一 event id，MUST NOT 重新调用 provider。Service worker MUST 在 DBOS runtime 启动前恢复全部已有确定结果，并在 queued run 重放或执行前再次执行 run-scoped recovery；两处恢复都 MUST 只处理 model/embedding operation kind，不得误消费 approval 等共享 outbox 项。每条 run-scoped `model.usage.updated` 的 `seq` MUST 小于同一 run 的 terminal event `seq`；runtime 发布 terminal 前 MUST 恢复或确定性封闭所有已开始的 usage 调用，未知结果 MUST 保持 pending/needs_review并阻止 terminal，EventBus/sink MUST 拒绝 terminal 后的任何后续业务事件。

#### Scenario: Composition 生成稳定且不含敏感输入的调用 ID
- **WHEN** 同一 durable run 的相同语义调用槽位因进程重启、DBOS 重放或请求重试再次进入 model/embedding invocation
- **THEN** composition 生成与首次逐字一致的 `usage_call_id` 并复用既有 settlement；不同槽位生成不同 ID，调用方不能传入随机 ID，也不能把 prompt、embedding input 或 secret 纳入 ID

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

#### Scenario: Worker 启动与 queued run 执行前分层恢复
- **WHEN** service worker 启动，或即将重放/执行某个 queued run，且共享 outbox 含 model、embedding 与 approval 等不同 operation kind 的确定结果
- **THEN** worker 在 DBOS runtime 启动前补投全部 model/embedding 确定结果，并在该 queued run 执行前再次只补投其 run-scoped model/embedding 结果；approval 项不被 usage recovery 消费，provider 不重放

### Requirement: Local fake run 满足入口时延门禁
local profile SHALL 使用无网络依赖的 fake provider，从公开单 agent run 入口到唯一 terminal 记录 monotonic 总时延并执行小于 5 秒的稳定门禁。验证 evidence MUST 输出可定位的阶段时延和 run/trace 关联，单元测试内部微步骤墙钟不得替代该入口证据。

#### Scenario: Local fake run 在阈值内完成
- **WHEN** 固定 local fixture 从公开入口创建并完成 single-agent fake run
- **THEN** smoke 分别读取同一 run 的 run terminal 与调用级最终 usage evidence，总时延小于 5 秒且无真实 API key 或外部网络依赖

#### Scenario: 时延超限可定位
- **WHEN** 入口到 terminal 总时延达到或超过 5 秒
- **THEN** smoke 非零失败并输出不含 secret 的阶段时延与关联标识，不以跳过、放宽阈值或单元测试结果替代

### Requirement: 真实非流式模型调用以调用级 reservation 和逐 attempt 证据收敛
每次受控真实模型调用 SHALL 在既有稳定 `usage_call_id` 和 durable settlement 内只建立一个调用级 reservation，并在 `ModelUsageEvidence.decision` 中保存冻结 route、调用级预算上界及有序 attempt summaries。真实 deployment 的 allowed model MUST 只引用 `ModelSettings.model_catalogs` 中 exact canonical ref/version；`config/model_catalog.py` 解析并校验 entry 的 provider/model、`request_shape_ref=single-user-text-no-tools`/`v1`、`input_bound_strategy_ref=utf8-bytes-plus-envelope`/`v1`、envelope bound、价格、price-source identity 与 canonical digest，deployment、Agent、request 或 provider 返回值不得自证、覆盖或补齐这些权威值。Route 计划 MUST 先拒绝超过 deployment `max_prompt_utf8_bytes` 的 prompt，再用解析后的 strategy 与 checked arithmetic 计算 `trusted_input_token_bound=len(prompt UTF-8 bytes)+input_envelope_token_bound`；真实 request 的 `max_output_tokens` MUST 在 `1..deployment.max_output_tokens` 内并冻结为 adapter 不可放大的 `output_token_cap`。Deployment 静态 max token/cost ceiling MUST 先按 typed-config 公式验证；具体 route 的动态 `per_attempt_token_bound` MUST 等于 `trusted_input_token_bound+output_token_cap`，cost 启用时动态 `per_attempt_cost_bound` MUST 等于 `trusted_input_token_bound*input_token_price_usd+output_token_cap*output_token_price_usd` 的有限非负 Decimal 结果，且两项价格、非空 price-source identity 与 cost bounds 必须一致冻结；cost disabled 时两项价格、price-source identity 和全部 cost bounds 只能按 typed catalog 的禁用语义为 null，不得被解释为零价或沿用陈旧来源。Catalog ref/version/provider/model/request-shape/strategy/price/source/digest 任一未知或不匹配、deployment 复制值低报或高报、静态 ceiling 低报或高报、动态公式不一致、prompt/output 越界、非有限或溢出时 MUST 在 reservation、Bulkhead、client lease 获取/构造和 provider 前返回 `config.invalid` 或 `budget.reservation_rejected`，且本调用 reservation count、client-construction delta、network/provider call count 均为零。调用级 reservation MUST 再以动态 `per_attempt_bound * max_attempts` 的 checked arithmetic 计算。Route、budget snapshot、operation identity 与公开 evidence MUST 冻结相同 catalog ref/version/digest 和解析值；reload 只影响新 root，恢复不得读取 current catalog 补齐或改价。Adapter MUST 把冻结 `output_token_cap` 作为 Pydantic AI `ModelSettings(max_tokens=...)` 传给真实 provider，不得从 request/settings 重算或放大。Summary MUST 只包含 API Contract 5.29 规定的脱敏字段，MUST NOT 包含 prompt、response raw body、header、credential、完整 URL、SDK exception 或 SDK object。

每个 attempt 的 token/cost 维度 SHALL 分别按以下穷举矩阵处理：可证明 `side_effect_state=not_started` 时该 attempt 为零；`side_effect_state=started` 且有该维度可信 provider usage/cost 时纳入 actual 聚合；`side_effect_state=started` 且任一已启用维度无可信 actual 时，无论 outcome 是 `completed`、确定性 `failed` 还是 `retryable_status`，整个调用都必须保持原调用级 reservation、进入 `needs_review` 并阻止 terminal，不得用 attempt 上界替代 actual、不得退款；`side_effect_state=unknown` 或完成状态不明时遵循相同未决规则。Cost disabled 时该维度不参与 reservation/needs-review，但不得影响 token 维度判断。`completed+not_started` 等不可能组合、actual 超过 reservation 或 evidence 自相矛盾时 MUST fail closed 为 `needs_review`，impact 遵循 shared-parent-budget-ledger 的 `max(original_reservation,trusted_actual)`，不得增加可用余额。

最终成功前的失败 attempts MUST 与成功 attempt 一并聚合。只有每个已启用维度在所有 `started` attempts 中都有可信 actual，且没有 unknown/invalid/actual-over，才能用聚合 actual 原子替换原 reservation、退还差额并允许 completed 或确定性 failed terminal；否则原调用 reservation 或更高可信 actual impact 保持未决，owner 的新预算 operation 与 terminal 均被阻止。顶层 token 字段仅在全部 `started` attempts 都有可信 token actual 时等于其总和，否则为 null；cost 全部可信时按 reported/estimated 规则聚合，否则为 null/`unavailable`。已知的部分 actual 可保留在 attempts evidence 中，但不得据此释放未决维度。禁止用零、attempt 上界或最终一次 usage 冒充调用总消耗。

#### Scenario: 首次 attempt 成功
- **WHEN** provider 在首次非流式请求中返回完整文本和合法 usage
- **THEN** 最终 evidence 记录一个 completed attempt、真实 latency/token/cost、同一冻结 route 和等于实际值的 budget charge，settlement/terminal 顺序保持不变

#### Scenario: Retryable status 后成功并聚合计费
- **WHEN** endpoint policy/version 绑定 `trusted_response_header_not_started/v1`，首次 attempt 返回 policy 显式列明的 429/5xx 与 exact 受信单值 header，transport 产出 `completion_observed=false`、没有 response id/usage/部分结果，随后 attempt 成功
- **THEN** 系统不新建第二个 reservation；首次 started attempt 缺 actual 使原调用 reservation 保持未决，即使后续成功也进入 `needs_review`、顶层 token 为 null且阻止 terminal，不按成功 attempt 或 attempt 上界退款

#### Scenario: 已完成文本但 provider 未返回 usage
- **WHEN** attempt 已返回完整文本并确定 `completed`，但没有可信 token/cost usage
- **THEN** 已知文本结果可写入耐久 provider-neutral result，但原调用 reservation 保持未决，顶层缺失维度为 null，settlement/owner 进入 `needs_review` 并阻止 terminal，不用估算或上界冒充 actual

#### Scenario: 确定性失败但 provider 未返回 usage
- **WHEN** attempt 已开始并以不可重试 HTTP/adapter 错误确定性失败，但没有可信 token/cost usage
- **THEN** provider outcome 以 `model.provider_failed` 记录，但原调用 reservation 保持未决，settlement/owner 进入 `needs_review` 并阻止 failed terminal；只有可证明整个调用没有 provider 副作用时才可释放

#### Scenario: Evidence 组合非法
- **WHEN** attempt 声称 `completed+not_started`、unknown 却携带互相冲突的完成状态，或 charge 与 usage availability 不符合穷举矩阵
- **THEN** settlement fail closed 为 needs-review，保留原 reservation 或更高可信 actual impact、阻止 terminal，SQLite 与 PostgreSQL 不得自行选择默认退款规则

#### Scenario: Reservation 上界无效
- **WHEN** prompt/output cap 越界、输入 strategy 未受 catalog 认证、配置的每 attempt 上界低报或高报、token/cost 公式不一致、`per_attempt_bound * max_attempts` 溢出/非有限/超出共享预算，或 cost hard limit 下缺少可信价格
- **THEN** 调用以 `budget.reservation_rejected` 在 reservation、Bulkhead、durable side-effect mark、client lease 获取/构造和 provider call 前失败，本调用 reservation count、client-construction delta、network/provider call count 均为零，且 cost 不被当作 0

#### Scenario: 冻结输出 cap 被 adapter 强制执行
- **WHEN** 合法真实 route 形成 `output_token_cap` 并进入 Pydantic AI adapter
- **THEN** provider double 观察到 `Agent.run(..., model_settings=ModelSettings(max_tokens=output_token_cap))`，其值与 route/evidence 逐字相同；adapter 不读取 mutable request/settings 放大 cap

### Requirement: 重试只基于显式完成状态并受同一 deadline 约束
Adapter SHALL 逐状态配置可重试的 429/5xx，但只有冻结 endpoint policy 显式绑定 ref=`trusted_response_header_not_started`、version=`v1`，且私有 transport 从该 endpoint 的原始 response 中取得恰好一个大小写不敏感名称 `X-Agent-Harness-Completion-State`、去除 OWS 后值逐字为 `not-started` 的 header 时，才能产出 `completion_observed=false` 并重试；该 attempt 的 `side_effect_state` 仍为 `started`。Header 缺失、重复、逗号合并、多值、畸形、其他值、非绑定 endpoint policy/version，或只在 body/SDK exception message 中出现时 MUST 分类为 `unknown`。未配置 classifier 的 deployment（包括默认官方 OpenAI endpoint policy）MUST 使用空 `retryable_http_statuses`，不得对 429/5xx response 自动 retry；只有 transport 能证明 request 尚未发送时才可标记 `not_started` 并在 `max_attempts` 内重试。`Retry-After`/backoff MUST 同时受单次等待上限、剩余 attempt 数与冻结 total deadline 约束。已观察到成功、部分业务结果、provider response id 或 usage MUST 覆盖任何 false header 为 observed/unknown，read timeout、取消和含糊连接错误也 MUST NOT 自动 retry/fallback。

#### Scenario: 显式 retryable status 后成功
- **WHEN** endpoint policy/version 绑定受信 classifier，provider 返回配置列明的 status 与 exact 单值 `not-started` header，没有 response id/usage/部分结果，且受限等待和下一 attempt 都能落在剩余 total deadline 内
- **THEN** adapter 将该 attempt 标为 started 且 usage-unresolved，等待后使用同一调用级 reservation、同一 Bulkhead permit 和同一 durable side-effect mark 发起下一 attempt；后续成功也不能解除既有 `needs_review` fencing

#### Scenario: 默认 endpoint 不虚构 completion signal
- **WHEN** 默认官方或其他未绑定 classifier 的 endpoint 返回 429/5xx，即使 body、exception message 或未受信 header 声称 completion 未开始
- **THEN** adapter 不自动重试，将完成状态归为 unknown/不可信，并按 started attempt 的未决结算规则保持 reservation；配置非空 response retry status 时 startup fail closed

#### Scenario: Completion signal 缺失、畸形或被结果覆盖
- **WHEN** classifier header 缺失、重复、多值、逗号合并、值不等于 `not-started`、来自非绑定 policy/version，或 response 同时含 response id、usage 或部分业务结果
- **THEN** transport/adapter 不产出可信 false、不发起下一 attempt；已有 id/usage/result 优先归类为 observed，其他情况归为 unknown，raw header/body 不进入 evidence

#### Scenario: Retry-After 超出剩余 deadline
- **WHEN** status 可重试但受限 `Retry-After` 或下一 attempt 已无法落入剩余 total deadline
- **THEN** adapter 不再发请求，以 `model.provider_retry_exhausted` 收敛；任一 started attempt 缺 actual 时保留原调用 reservation并进入 `needs_review`

#### Scenario: 可重试失败耗尽 attempt 上限
- **WHEN** 受信 429/5xx 或可证明 not-started 的 transport 失败持续到冻结 `max_attempts`
- **THEN** adapter 不再发请求，以携带全部安全 attempts 的 `model.provider_retry_exhausted` 收敛，不得透出最后一次内部 `model.provider_failed`

#### Scenario: 完成状态不明确时禁止重试
- **WHEN**发生 read timeout、外部取消、无法证明请求未发送的连接错误，或 response 已包含业务结果、response id 或 usage
- **THEN** adapter 不发起下一 attempt；未知完成状态使用 `model.provider_side_effect_unknown`，已完成但失败的状态使用稳定的 `model.provider_failed`

### Requirement: 模型 soft policy 与审批复用既有耐久 continuation
Runtime SHALL 在 immutable route 动态 hard eligibility 后、任何 model reservation 前调用既有 `PolicyEngine.evaluate(PolicyCheck)`。`PolicyCheck.actor` MUST 使用 bound execution identity，`action` MUST 为 `model.invoke`，`resource` MUST 为 `agent:<agent_id>:model`，安全 context 只含 tenant/run/agent/request/trace、冻结 route/catalog identity、reservation bounds 与 soft-limit decision。既有 `AuditService` MUST 为 allow/deny/require-approval 写入 `policy.decision` 并返回 `audit_ref`；该 audit 不新增 CanonicalEvent、不占 event capacity，只证明策略判断，不得冒充 provider 已开始。

`require_approval` MUST 复用既有 `AgentApprovalRequest`、`policy_approval` checkpoint、`ApprovalRecord`、resolution lease、`ApprovalGrant`、`ApprovalService` 和 continuation resume 链，冻结并校验 approval/lease/tenant/identity/agent/run、`action=model.invoke`、`resource=agent:<agent_id>:model` 与 canonical `ModelRequest` arguments hash。等待审批时 reservation/permit/client/durable mark/network 均为零。获批 continuation MUST 先校验 durable record 状态、单次 lease 与全部绑定，再通过 `BoundModelInvocationService.complete_approved()` 跳过同一个 soft decision；调用方不得提交公开 `approved` bool。获批路径 MUST 重新执行当前 route/catalog hard eligibility 与 owner shared hard balance，approval 只能继续原 intent 或进一步缩权，不能提高、重置或覆盖 hard limit。稳定 operation identity MUST 绑定 approval id；mismatch、stale、duplicate/replay 均 fail closed，获批 continuation 最多调用 provider 一次，崩溃恢复只恢复既有 settlement，不重新请求 provider。

#### Scenario: Policy audit 不等于 provider 副作用
- **WHEN** policy 返回 allow、deny 或 require-approval
- **THEN** runtime 写入既有 `policy.decision` audit；deny 与等待审批路径的 reservation、permit、client、durable mark 与 network delta 均为零，audit ref 不得被解释为 provider started

#### Scenario: Require-approval 建立耐久等待点
- **WHEN** exact model policy decision 为 require-approval
- **THEN** runtime 创建绑定完整 execution identity、action/resource 与 request hash 的既有 checkpoint/ApprovalRecord，挂起 run 且不建立 model reservation；不得创建第二套模型审批状态机或新授权事件

#### Scenario: 完整绑定的 ApprovalGrant 恢复一次调用
- **WHEN** durable approval 已 resolved，resolution lease 有效且 `ApprovalGrant` 的全部字段与挂起请求逐字匹配
- **THEN** continuation 单次消费 lease，重新执行 hard route/catalog/current owner balance 后，按原冻结 intent 最多调用 provider 一次；稳定 approval id 参与 operation identity，重放只恢复原 settlement

#### Scenario: 不匹配、过期或重放 grant fail closed
- **WHEN** approval/lease/tenant/identity/agent/run/action/resource/arguments hash 任一不匹配，record 不是有效 resolved 状态，lease 已消费或 continuation 重放试图再次开始 provider
- **THEN** runtime 在 reservation/permit/client/mark/network 前拒绝，不把调用方 bool 或旧 audit 当作批准，也不提高 shared hard limit

### Requirement: Provider-neutral permit 固定 Bulkhead 与耐久副作用顺序
Runtime SHALL 通过 provider-neutral `PreparedModelCall`/`ModelExecutionPermit` 接缝执行以下固定顺序：immutable route 动态 hard eligibility → 上述 soft policy/fallback/approval 协调与既有 `policy.decision` audit → 预算与 settlement reservation → 取得 deployment Bulkhead permit（不得联网）→ lazy client factory 获取/构造绑定 frozen route 的 client lease（构造不得联网）→ durable `side_effect_started` mark → adapter send/execute。Soft policy deny 或 approval-required 未获批准时 reservation/permit/client/mark/network 均为零；approval 只能缩小或继续冻结 intent，不得提高、重置或覆盖 shared hard limit。重试 MUST 在同一 permit、reservation、client lease 和 durable mark 内完成。Vendor client/request 对象不得越过 adapter boundary。动态 eligibility 失败时 reservation/client/network 均为零；client 构造失败时不得提交 durable mark，必须按 not-started 回滚 reservation/permit，最终 active reservation、mark 与 network delta 均为零；队列超时在 client/mark 前返回 `model.bulkhead_saturated`；durable mark 后任何无法证明未送达的中断都按 unknown 封闭。此前已写入的 `policy.decision` audit 保留原始 decision，但不得改写或解释为 provider 已开始。

#### Scenario: Soft policy deny 与未批准零模型副作用
- **WHEN** immutable route hard eligibility 已通过，但 soft policy deny、要求 approval 或批准尚未完成
- **THEN** runtime 保持 reservation、permit、client construction/acquisition、durable mark 与 network delta 全部为零；只允许既有 `policy.decision` audit 记录原始 decision，approval 不得改变 owner shared hard limit，只有获准的原 intent 或更小 route 才能继续

#### Scenario: Bulkhead 饱和零网络副作用
- **WHEN** deployment 已达到 `max_in_flight` 且调用在 `queue_timeout` 内未取得 permit
- **THEN** 调用返回 `model.bulkhead_saturated`，记录 `not_started`，provider call count 为零，并释放或取消尚未开始的 reservation

#### Scenario: Client lease 构造失败不伪造 provider started
- **WHEN** hard eligibility、policy decision、reservation 与 Bulkhead permit 已完成，但 lazy client factory 构造 client lease 失败
- **THEN** runtime 不写 durable side-effect mark、不发网络；回滚 reservation/permit并记录 not-started，最终 active reservation、mark 与 network delta 均为零，已部分构造资源被幂等关闭；既有 policy audit 仍只表示原始 decision

#### Scenario: 取得 permit 后、durable mark 前崩溃
- **WHEN** worker 已取得 permit 但尚未写入 durable `side_effect_started` 时崩溃
- **THEN** permit 随进程释放，恢复仍观察到 reserved/not_started，可按既有幂等规则安全重放且不会重复计费

#### Scenario: Durable mark 后、实际 send 前崩溃
- **WHEN** durable `side_effect_started` 已提交但进程在 adapter send 前崩溃
- **THEN** 恢复保守归类为 `model.provider_side_effect_unknown`/`needs_review`，不自动重放、不释放未决 reservation；允许假阳性，禁止假阴性重复副作用

#### Scenario: Attempts 之间崩溃
- **WHEN**一个显式 retryable attempt 已记录而下一 attempt 尚未完成时进程崩溃
- **THEN** 恢复按 started/unknown 封闭整个调用，不从 attempt 序号继续自动请求 provider

#### Scenario: 已结算稳定失败的调用重放
- **WHEN** 相同 `usage_call_id` 重放已经持久化的 `model.bulkhead_saturated`、`model.invocation_cancelled`、`model.provider_failed`、`model.provider_retry_exhausted` 或 `model.provider_side_effect_unknown` 结果
- **THEN** runtime 的公开调用重放与后台恢复先共用完整 settlement validator 校验 evidence、outcome、稳定 error identity 与 failure/response，再允许发布 final event/telemetry 或把 outbox 标记为 `published`；合法稳定失败不再次调用 provider，只从 exact `error_code/provider_called/attempt_count/latency_ms` failure evidence 恢复同一 code 与原调用事实；稳定错误与 response 共存、failure 缺失/多余字段、`provider_called` 与 attempt 数不一致、非法 latency、evidence/outcome 缺失或畸形、畸形 response 均统一 fail closed 为 replay error，且零 final publication、零状态推进，不猜测为零调用、不返回伪造成功，也不泄漏普通/Pydantic 校验异常

### Requirement: Deadline 与取消保持副作用未知语义
Provider execution SHALL 使用异步可取消 I/O，并让 Bulkhead 排队、connect/read timeout、retry wait 与全部 attempts 共同受冻结 total deadline 约束。调用在 durable `side_effect_started` mark 前收到外部取消时 MUST 取消未完成的本地工作，释放或归还已取得的 client lease 与 Bulkhead permit，回滚本调用 reservation，保持零 durable mark 与零 provider 网络副作用，并以 `model.invocation_cancelled` 收敛；此前的 `policy.decision` audit 不得冒充 provider 已开始。调用进入 durable side-effect 区间后的 read/total timeout、外部取消或含糊连接异常 MUST 以 `model.provider_side_effect_unknown` 收敛，禁止自动 retry/fallback、禁止释放或按零结算保守预算，并保持 pending/needs-review terminal fencing。

#### Scenario: Durable mark 前取消确定性回滚
- **WHEN** worker 或调用方在 policy decision、reservation、Bulkhead 排队/持有 permit或 client lease 构造/持有后取消 model task，但 durable `side_effect_started` 尚未提交
- **THEN** runtime 取消尚未完成的本地工作，释放或归还 client lease 与 permit，回滚 reservation，记录 `not_started` 并返回 `model.invocation_cancelled`；durable mark 与 provider network delta 均为零，不自动 retry/fallback

#### Scenario: Total deadline 在 durable mark 后到达
- **WHEN** total deadline 在 durable `side_effect_started` 后到达且无法证明服务端未处理
- **THEN** 当前 async I/O 被取消，settlement 记录 unknown usage/cost 与未决预算，不启动下一 attempt、不自动 fallback fake

#### Scenario: 外部取消不转换为普通失败
- **WHEN** worker 或调用方取消一个 durable side-effect 已开始的 model task
- **THEN** adapter 保留取消语义供 runtime 处理，同时耐久记录 side-effect unknown evidence；不得吞掉取消、改写为成功或重新调用 provider

### Requirement: 默认离线验证与 opt-in live evidence 分离
默认 quality、unit、contract、eval 和 smoke-local SHALL 只使用 fake/provider doubles，MUST NOT 读取真实 credential、构造真实 DNS/HTTP 请求或依赖外部网络。真实 provider smoke MUST 由独立显式 opt-in 开关、隔离非生产 credential、受信 endpoint 和单独用户授权同时启用；缺任一条件时结果 MUST 唯一为 `hosted-unverified` 并映射为 skipped evidence，MUST NOT 标记 PASS。只有上述条件齐全且已获授权后，外部网络或 provider 阻断才可标记 `external-blocked`。

#### Scenario: 默认验证拒绝意外联网
- **WHEN** 默认 CI/local 验证运行且宿主机恰好存在 provider 原生 key 或代理环境变量
- **THEN** 测试仍只调用 fake/double，网络哨兵观察到零外部连接，secret 未被读取

#### Scenario: 未授权 live smoke 准确跳过
- **WHEN** 没有单独用户授权、opt-in 开关、隔离 credential 或可用受信 endpoint
- **THEN** live smoke 不发起请求并输出脱敏 `hosted-unverified`，进程退出 0 并映射 `ci-result/v1 status=skipped`；不得用 `external-blocked` 或 PASS 表达缺少本地前置条件

#### Scenario: 已授权 live smoke 只暴露脱敏证据
- **WHEN** 调用方另行授权并提供隔离 credential、opt-in 开关和受信 endpoint
- **THEN** smoke 最多执行契约允许的单次非流式 completion 流程，只保存 route/attempt/usage/latency/status 摘要，不保存 prompt 全文、输出全文、raw response 或 secret

#### Scenario: provider 失败仍按事实报告调用摘要
- **WHEN** 已授权 live smoke 遭遇 429/5xx、retry exhausted、read-timeout 或 side-effect unknown，因而没有成功 response
- **THEN** smoke 从封闭的 invocation error/evidence 保留真实 `provider_called`、`attempt_count` 与安全 latency，不读取 raw SDK 异常，也不得把已发请求改写为零调用
