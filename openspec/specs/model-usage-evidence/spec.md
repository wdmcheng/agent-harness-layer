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
Legacy 单 route审批续跑 SHALL 继续使用 `approved:{approval_id}` 语义槽位。显式 route chain MUST 在首次可信入口、approval record出现前就从受信上下文与原始语义 operation key生成并冻结唯一 `usage_call_id` 和 `operation_identity_digest`；waiting state、approval request/record/grant、activation、settlement/outbox及 streaming group都复用同一 ID。approved continuation只能从私有 checkpoint重算并逐值匹配，MUST NOT 改用 approval id rekey、创建映射或第二 claim；不匹配时 reservation、capacity和 provider调用均为零。

系统 SHALL 在 provider 副作用前生成稳定 `usage_call_id` 并发布 `model.request.started`，在完成、受控拒绝或 provider 失败后发布恰好一条调用级最终 `model.usage.updated`。Composition MUST 从 durable tenant/run/request/agent/trace 关联与稳定的语义调用槽位生成该 ID；invocation seam MUST NOT 随机回退，也 MUST NOT 接受 prompt、embedding input、secret 或其他敏感业务输入作为调用槽位。Model 与 embedding 精确复用这两个 event type，并以 `ModelUsageEvidence.usage_kind` 区分，不得新增等价 embedding event。Started 与最终 usage event MUST 逐值共享 tenant/run/request/agent/trace 和 `usage_call_id` correlation。Legacy 单 route 还 MUST 逐值共享 provider/model；显式 route chain 只有在 started/final都携带逐值相同的合法 `decision.route_chain` identity、所有前序候选逐项满足“零 attempt的 `static_ineligible|budget_ineligible` state”或“全部实际 attempts以 `client_not_started|trusted_business_not_started`完整有序 proof records和连续原子 transition安全收敛”，且每份 provider/model逐值命中各自 `evidence_route_ordinal`时，才 MAY不同。公开 started event本身不围栏 provider route；除逐 attempt完整 trusted-business proof外，provider started/HTTP response后不得再改变 provider/model。Completed的 final ordinal必须等于 selected；denied/exhausted的 selected保持 null但 final route仍由 denied/last-cause evidence ordinal唯一确定。`model.usage.updated`只结束该 `usage_call_id`，其 `CanonicalEvent.terminal` MUST为 false，run terminal marker仍只允许 `run.completed`、`run.failed`、`run.cancelled`。`usage_call_id`继续属于 CanonicalEvent/telemetry metadata，不新增顶层 `ModelUsageEvidence.usage_call_id`；显式 chain的 `decision.route_chain.state.usage_call_id`是完整 state既定的唯一嵌套公开字段，并须与 envelope/telemetry correlation逐值相同。失败 event MUST保留已知 latency、usage和 route/budget decision，并通过 CanonicalEvent envelope使用稳定、脱敏 error code/summary。

每次 started 调用 MUST 在 provider 副作用前建立以 `(tenant_id, usage_call_id)` 唯一的 durable settlement/outbox 与稳定 usage event id；provider 结果、脱敏 usage 摘要或确定性失败 MUST 只写入该状态一次。sink 写入失败、确认丢失或进程重启后，恢复 MUST 从已持久化结果幂等补投同一 event id，MUST NOT 重新调用 provider。Service worker MUST 在 DBOS runtime 启动前恢复全部已有确定结果，并在 queued run 重放或执行前再次执行 run-scoped recovery；两处恢复都 MUST 只处理 model/embedding operation kind，不得误消费 approval 等共享 outbox 项。每条 run-scoped `model.usage.updated` 的 `seq` MUST 小于同一 run 的 terminal event `seq`；runtime 发布 terminal 前 MUST 恢复或确定性封闭所有已开始的 usage 调用，未知结果 MUST 保持 pending/needs_review并阻止 terminal，EventBus/sink MUST 拒绝 terminal 后的任何后续业务事件。

#### Scenario: Composition 生成稳定且不含敏感输入的调用 ID
- **WHEN** 同一 durable run 的相同语义调用槽位因进程重启、DBOS 重放或请求重试再次进入 model/embedding invocation
- **THEN** composition 生成与首次逐字一致的 `usage_call_id` 并复用既有 settlement；不同槽位生成不同 ID，调用方不能传入随机 ID，也不能把 prompt、embedding input 或 secret 纳入 ID

#### Scenario: 完成路径恰好一次结算
- **WHEN** provider 调用成功完成
- **THEN** 同一调用只有一条最终 usage evidence，并可由 started correlation 找到，不因 telemetry fan-out 重复结算，且不会以 run terminal marker 提前关闭事件流

#### Scenario: Fallback 调用实际备用 provider
- **WHEN** router 在 provider 调用前选择 legacy fallback route，或显式 chain 在 `client_not_started|trusted_business_not_started` transition 后选择后继 candidate
- **THEN** 系统只调用选定/激活的 provider，调用级最终 usage evidence 记录原 route decision 与实际 provider/model，且 `CanonicalEvent.terminal=false`
- **AND** chain 模式的 started/final provider/model 差异只能由相同 immutable chain、连续 transition 和 selected ordinal 证明

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
每次受控真实模型调用 SHALL 在既有稳定 `usage_call_id` 和 durable settlement 内只建立一个调用级 reservation；显式 route chain 在任一时刻也只能有一个候选 reservation，并在 approval waiting 时使用零 impact coordination row。`ModelUsageEvidence.decision` SHALL 保存冻结 route、调用级预算上界及有序 attempt summaries；chain mode 还必须保存 exact `route_chain` identity/state。真实 deployment 的 allowed model MUST 只引用 `ModelSettings.model_catalogs` 中 exact canonical ref/version；`config/model_catalog.py` 解析并校验 entry 的 provider/model、`request_shape_ref=single-user-text-no-tools`/`v1`、`input_bound_strategy_ref=utf8-bytes-plus-envelope`/`v1`、envelope bound、价格、price-source identity 与 canonical digest，deployment、Agent、request 或 provider 返回值不得自证、覆盖或补齐这些权威值。Route 计划 MUST 先拒绝超过 deployment `max_prompt_utf8_bytes` 的 prompt，再用解析后的 strategy 与 checked arithmetic 计算 `trusted_input_token_bound=len(prompt UTF-8 bytes)+input_envelope_token_bound`；真实 request 的 `max_output_tokens` MUST 在 `1..deployment.max_output_tokens` 内并冻结为 adapter 不可放大的 `output_token_cap`。Deployment 静态 max token/cost ceiling MUST 先按 typed-config 公式验证；每个 candidate 的动态 `per_attempt_token_bound` MUST 等于 `trusted_input_token_bound+output_token_cap`，cost 启用时动态 `per_attempt_cost_bound` MUST 等于 `trusted_input_token_bound*input_token_price_usd+output_token_cap*output_token_price_usd` 的有限非负 Decimal 结果，且两项价格、非空 price-source identity 与 cost bounds 必须一致冻结；cost disabled 时两项价格、price-source identity 和全部 cost bounds 只能按 typed catalog 的禁用语义为 null，不得被解释为零价或沿用陈旧来源。Catalog ref/version/provider/model/request-shape/strategy/price/source/digest 任一未知或不匹配、deployment 复制值低报或高报、静态 ceiling 低报或高报、动态公式不一致、prompt/output 越界、非有限或溢出时 MUST 在 reservation、Bulkhead、client lease 获取/构造和 provider 前返回 `config.invalid` 或 `budget.reservation_rejected`，且本调用 reservation count、client-construction delta、network/provider call count 均为零。每个候选 reservation MUST 再以该候选动态 `per_attempt_bound * max_attempts` 的 checked arithmetic 计算；candidate transfer 只能原子替换，不能叠加。Route、budget snapshot、operation identity 与公开 evidence MUST 冻结相同 catalog ref/version/digest 和解析值；reload 只影响新 root，恢复不得读取 current catalog 补齐或改价。Adapter MUST 把冻结 `output_token_cap` 作为 Pydantic AI `ModelSettings(max_tokens=...)` 传给真实 provider，不得从 request/settings 重算或放大。Summary MUST 只包含 API Contract 5.29 与 route-chain delta 规定的脱敏字段，MUST NOT 包含 prompt、response raw body、header、credential、完整 URL、SDK exception 或 SDK object。

每个 attempt 的 token/cost 维度 SHALL 分别按以下穷举矩阵处理：可证明 `side_effect_state=not_started` 且 request/HTTP response 均未发生的 `client_not_started` attempt 为零；显式 chain 中 claim/candidate 可保持 started，但由端点绑定 classifier、冻结跨 provider 状态白名单、无 response identity/usage/text/delta 和该全局 attempt 的 durable proof record共同证明的 `trusted_business_not_started` attempt也为零，且不得回写 request/response/started 历史；`side_effect_state=started` 且有该维度可信 provider usage/cost 时纳入 actual 聚合；其他 `side_effect_state=started` 且任一已启用维度无可信 actual 时，无论 outcome 是 `completed`、确定性 `failed` 还是 `retryable_status`，整个调用都必须保持当前 reservation、进入 `needs_review` 并阻止 terminal，不得用 attempt 上界替代 actual、不得退款。上述零 charge 特例只属于显式 route chain，并要求 same-route retry 前已原子持久化上一 attempt proof，最终 transfer 时 candidate proof list 覆盖全部实际 attempts且与 evidence 逐值一致；legacy 同 route retry 即使有相同 header也继续按既有未决规则处理。`side_effect_state=unknown` 或完成状态不明时遵循相同未决规则。Cost disabled 时该维度不参与 reservation/needs-review，但不得影响 token 维度判断。`completed+not_started`、proof 缺失/覆盖/重排等不可能组合、actual 超过 reservation 或 evidence 自相矛盾时 MUST fail closed 为 `needs_review`，impact 遵循 shared-parent-budget-ledger 的 `max(original_reservation,trusted_actual)`，不得增加可用余额。

最终成功前的失败 attempts MUST 与成功 attempt 一并聚合。只有每个 started attempt 都有各启用维度可信 actual，且没有 unknown/invalid/actual-over，才能用聚合 actual 原子替换当前 reservation、退还差额并允许 completed 或确定性 failed terminal；否则当前 reservation 或更高可信 actual impact 保持未决，owner 的新预算 operation 与 terminal 均被阻止。顶层 token 字段仅在全部 started attempts 都有可信 token actual时等于其总和，否则为 null；cost 全部可信时按 reported/estimated 规则聚合，否则为 null/`unavailable`。已知的部分 actual 可保留在 attempts evidence 中，但不得据此释放未决维度。禁止用零、attempt 上界或最终一次 usage 冒充调用总消耗。

#### Scenario: 首次 attempt 成功
- **WHEN** provider 在首次非流式请求中返回完整文本和合法 usage
- **THEN** 最终 evidence 记录一个 completed attempt、真实 latency/token/cost、同一冻结 route 和等于实际值的 budget charge，settlement/terminal 顺序保持不变

#### Scenario: Legacy retryable status 后成功仍保持未决
- **WHEN** legacy 单 route endpoint 绑定 `trusted_response_header_not_started/v1`，首次 attempt 返回受信 header 后在同一 route retry并成功
- **THEN** 系统不新建第二个 reservation；首次 started attempt 缺 actual 使原调用 reservation 保持未决，即使后续成功也进入 `needs_review`、顶层 token 为 null且阻止 terminal

#### Scenario: Chain candidate client 前切换
- **WHEN** 显式 chain 的首 candidate 在 request started/send 前以 `client_not_started` 收敛，随后原子 transfer 到次 candidate并成功
- **THEN** 首 attempt 保留 `side_effect_state=not_started`、request 未发送且 charge 为 0，次 attempt 记录实际 usage/cost
- **AND** final 顶层聚合为次 attempt actual，route 差异由同一 chain identity、transition 和 selected ordinal 逐值证明

#### Scenario: Chain candidate 受信业务未开始切换
- **WHEN** 显式 chain 的首 candidate 保持 started/request-sent/response-observed 历史，但状态、classifier、无生成计量事实与 proof digest 逐值证明 `trusted_business_not_started`，随后原子 transfer 到次 candidate并成功
- **THEN** 首 attempt 以 actual zero 结算且不伪造为未发送，次 attempt 记录实际 usage/cost
- **AND** legacy 单 route 的相同 header仍保持原调用 reservation并进入 needs-review，不获得该 chain-only 例外

#### Scenario: Chain candidate 多次受信 retry 的逐 attempt 结算
- **WHEN** 同一 chain candidate 连续两个 attempts 都以完整 `trusted_business_not_started` 收敛，第一条按冻结策略触发同 route retry，第二条耗尽后触发 transfer
- **THEN** 两个全局 attempt 各有不可覆盖的 proof record、attempt evidence 和零 charge，candidate 聚合高水位保持 started
- **AND** SQLite/PostgreSQL recovery 只从下一尚未开始的 attempt 或已提交 transfer 继续；任一 record 缺失、覆盖、重排、字段冲突或提交确认未知都进入 needs-review且不重放 provider

#### Scenario: 已完成文本但 provider 未返回 usage
- **WHEN** attempt 已返回完整文本并确定 `completed`，但没有可信 token/cost usage
- **THEN** 已知文本结果可写入耐久 provider-neutral result，但原调用 reservation 保持未决，顶层缺失维度为 null，settlement/owner 进入 `needs_review` 并阻止 terminal，不用估算或上界冒充 actual

#### Scenario: 确定性失败但 provider 未返回 usage
- **WHEN** attempt 已开始并以不可重试 HTTP/adapter 错误确定性失败，但没有可信 token/cost usage
- **THEN** provider outcome 以 `model.provider_failed` 记录，但原调用 reservation 保持未决，settlement/owner 进入 `needs_review` 并阻止 failed terminal；只有可证明整个调用没有 provider 副作用时才可释放

#### Scenario: Evidence 组合非法
- **WHEN** attempt 声称 `completed+not_started`、unknown 却携带互相冲突的完成状态、started/response 后缺少完整 chain-only trusted proof却伪造零 charge，或 charge 与 usage availability 不符合穷举矩阵
- **THEN** settlement fail closed 为 needs-review，保留原 reservation 或更高可信 actual impact、阻止 terminal，SQLite 与 PostgreSQL 不得自行选择默认退款规则

#### Scenario: Reservation 上界无效
- **WHEN** prompt/output cap 越界、输入 strategy 未受 catalog 认证、任一 candidate 的每 attempt 上界低报或高报、token/cost 公式不一致、`per_attempt_bound * max_attempts` 溢出/非有限/超出共享预算，或 cost hard limit 下缺少可信价格
- **THEN** 调用以 `budget.reservation_rejected` 在 reservation、Bulkhead、durable side-effect mark、client lease 获取/构造和 provider call 前失败，本调用 reservation count、client-construction delta、network/provider call count 均为零，且 cost 不被当作 0

#### Scenario: 冻结输出 cap 被 adapter 强制执行
- **WHEN** 合法真实 candidate 形成 `output_token_cap` 并进入 Pydantic AI adapter
- **THEN** provider double 观察到 `Agent.run(..., model_settings=ModelSettings(max_tokens=output_token_cap))`，其值与 route/evidence 逐字相同；adapter 不读取 mutable request/settings 放大 cap

### Requirement: 重试只基于显式完成状态并受同一 deadline 约束
Adapter SHALL仅按冻结endpoint policy和显式状态处理retry。Legacy单route逐字保持Phase 18：只有endpoint绑定`trusted_response_header_not_started/v1`且私有transport从原始response取得唯一exact `X-Agent-Harness-Completion-State: not-started`、无response id/usage/partial result时，才在同一调用级reservation、Bulkhead permit、client lease和durable side-effect mark内执行同route retry；该started attempt仍缺actual，后续成功也保持needs-review。Legacy受信status耗尽`max_attempts`时以`model.provider_retry_exhausted`终止。

显式route chain将同一endpoint-bound classifier与deployment冻结`cross_provider_failover_http_statuses`共同用于`trusted_business_not_started`，403默认不启用；每次同route retry MUST先在同一UoW把上一attempt proof与lifecycle关闭为`not_started_proven`，再创建新的全局attempt started identity，不能复用permit/client/mark identity。同route retry优先于跨provider；最后一次安全proof耗尽当前candidate attempts后，若调用未超deadline且owner UoW可按冻结ordinal提交transfer，runtime MAY推进首个eligible后继，而不是无条件以`model.provider_retry_exhausted`结束。没有eligible后继时按`model.route_chain_exhausted`的canonical terminal收口。任一started悬空、proof/transfer未知、非受信response、response identity、usage/text/delta、write/read timeout、取消或deadline耗尽都禁止retry和跨provider，保留reservation并needs-review或按可信actual结算。

两种模式都要求`Retry-After`/backoff受单次等待上限、剩余attempt和冻结total deadline约束；默认官方或未绑定classifier的deployment保持空response retry statuses。Header缺失、重复、逗号合并、多值、畸形、来源不匹配，或只在body/exception出现时均为unknown；已观察成功、部分结果、provider response id或usage覆盖任何false header。Raw header/body不进入evidence。

#### Scenario: Legacy 显式 retryable status 后成功
- **WHEN** legacy endpoint返回受信status与exact not-started header，并在剩余deadline内同route retry成功
- **THEN** adapter复用同一reservation、permit、client lease和durable mark；首次started attempt缺actual使调用保持needs-review

#### Scenario: Chain 显式 retryable status 后同 route 重试
- **WHEN** chain attempt收到白名单status及完整`trusted_business_not_started`证明，且冻结policy允许下一attempt并有剩余deadline
- **THEN** runtime先原子proof-close当前lifecycle，再创建下一全局started identity并调用同一candidate一次
- **AND** 两attempt identity/proof不可覆盖，permit/client可按candidate策略重新取得但不得复用attempt identity

#### Scenario: Chain 同 route retry 安全耗尽后 transfer
- **WHEN** 当前candidate的最后允许attempt也以完整not-started proof收敛，全部lifecycle均为`not_started_proven`，且存在eligible后继
- **THEN** owner UoW按冻结ordinal原子actual-zero并transfer到后继；后继先创建新started identity再调用一次
- **AND** 不发布`model.provider_retry_exhausted`，不重发当前candidate；无后继时才以route-chain exhausted终止

#### Scenario: 可重试失败耗尽 attempt 上限
- **WHEN** legacy单route的受信429/5xx或可证明not-started的transport失败持续到冻结`max_attempts`
- **THEN** adapter不再发请求，以携带全部安全attempts的`model.provider_retry_exhausted`收敛，不得透出最后一次内部`model.provider_failed`

#### Scenario: 默认 endpoint 不虚构 completion signal
- **WHEN** 默认官方或未绑定classifier的endpoint返回429/5xx，即使body、exception或未受信header声称未开始
- **THEN** 两种模式都不自动retry或fallback，按started/unknown规则保留reservation；配置非法非空status时startup fail closed

#### Scenario: Completion signal 缺失、畸形或被结果覆盖
- **WHEN** classifier header缺失、重复、多值、逗号合并、值错误、来源不匹配，或response含id、usage、text/delta
- **THEN** transport不产出可信false、不发下一attempt或provider；已观察事实优先，其他归unknown

#### Scenario: Retry-After 超出剩余 deadline
- **WHEN** status可重试但等待或下一attempt无法落入剩余total deadline
- **THEN** 两种模式都不再发请求；legacy按既有retry-exhausted/needs-review收口，chain按deadline围栏停止且不得跨provider

#### Scenario: 完成状态不明确时禁止重试
- **WHEN** 发生read timeout、外部取消、无法证明未发送的连接错误，或response已含业务结果、response id或usage
- **THEN** adapter不发下一attempt或provider；unknown使用`model.provider_side_effect_unknown`，可信失败按稳定error与actual结算

### Requirement: 模型 soft policy 与审批复用既有耐久 continuation
Runtime SHALL在 immutable route动态 hard eligibility后、任何 model reservation前调用既有 `PolicyEngine.evaluate(PolicyCheck)`。`PolicyCheck.actor` MUST使用 bound execution identity，`action` MUST为 `model.invoke`，`resource` MUST为 `agent:<agent_id>:model`，安全 context只含 tenant/run/agent/request/trace、当前候选冻结 route/catalog identity、reservation bounds与 soft-limit decision。每个 route-chain candidate独立检查；前一候选 allow/approval不得继承。既有 `AuditService` MUST为 allow/deny/require-approval写入 `policy.decision`并返回 `audit_ref`；该 audit不新增 CanonicalEvent、不占 event capacity，只证明策略判断，不得冒充 provider已开始。

`require_approval` MUST复用既有 `AgentApprovalRequest`、`policy_approval` checkpoint、`ApprovalRecord`、resolution lease、`ApprovalGrant`、`ApprovalService`和 continuation resume链。Legacy单 route继续让稳定 operation identity绑定 approval id，获批后 current balance不足仍按既有 hard reject。显式 route chain则在 approval前从原始可信 operation key生成并冻结 usage call id与 operation identity digest；waiting coordination state、checkpoint、ApprovalRecord/Grant、activation、settlement/outbox与 stream group都绑定同一身份。获批 continuation MUST先从私有 checkpoint重算并校验 durable record状态、单次 lease与全部绑定，再通过 `BoundModelInvocationService.complete_approved()`/`stream_approved()`跳过同一个 soft decision；调用方不得提交公开 `approved` bool或覆盖 identity。Chain approved路径复用原 claim并重新执行目标候选 route/catalog hard eligibility与 owner shared hard balance：余额足够才激活，余额不足则以 `budget_ineligible/balance`和 grant digest零 impact收敛，后继重新执行独立 Policy/HITL；禁止复用 grant、`approved:<approval_id>` rekey、identity mapping或第二 claim。Approval只能继续原 intent或进一步缩权，不能提高、重置或覆盖 hard limit。Mismatch、stale、duplicate/replay均 fail closed；获批 continuation最多调用 provider一次，崩溃恢复只恢复既有 settlement。

#### Scenario: Policy audit 不等于 provider 副作用
- **WHEN** 任一候选 policy返回 allow、deny或 require-approval
- **THEN** runtime写入既有 `policy.decision` audit；deny与等待审批路径的 reservation、permit、client、durable mark与 network delta均为零，audit ref不得被解释为 provider started

#### Scenario: Require-approval 建立耐久等待点
- **WHEN** exact model policy decision为 require-approval
- **THEN** runtime创建绑定完整 execution identity、action/resource与 request hash的既有 checkpoint/ApprovalRecord，挂起 run且不建立目标 model reservation；chain同时绑定 approval前 usage identity，不创建第二套模型审批状态机或新授权事件

#### Scenario: Legacy ApprovalGrant 恢复一次调用
- **WHEN** legacy durable approval已 resolved，resolution lease有效且 grant全部字段与挂起请求逐字匹配
- **THEN** continuation单次消费 lease，以 approval id绑定既有 operation identity，重算 hard route/catalog/current owner balance后最多调用 provider一次；重放只恢复原 settlement

#### Scenario: Chain ApprovalGrant 恢复原调用
- **WHEN** route-chain durable approval已 resolved且 grant绑定 approval前 usage identity
- **THEN** continuation从 checkpoint重算同一 ID，激活原 claim并最多调用目标 provider一次；waiting→record、lease→activation和 activation commit-ack恢复都不 rekey或重复预算影响

#### Scenario: Chain 获批目标余额不足的证据不漂移
- **WHEN** matching grant已提交，但目标候选在 activation时 current balance不足
- **THEN** evidence耐久记录目标 `budget_ineligible/balance`与 grant digest、零 attempt/charge/impact，后继使用新的独立 policy evidence
- **AND** crash replay不按新余额激活目标，也不把原 grant或 audit ref归给后继

#### Scenario: 不匹配、过期、伪造身份或重放 grant fail closed
- **WHEN** approval/lease/tenant/identity/agent/run/action/resource/arguments hash、chain usage identity任一不匹配，record不是有效 resolved状态，lease已消费或 continuation试图再次开始 provider
- **THEN** runtime在 reservation/permit/client/mark/network前拒绝，不把调用方 bool、旧 audit或新 operation key当作批准，也不提高 shared hard limit

### Requirement: Provider-neutral permit 固定 Bulkhead 与耐久副作用顺序
Runtime SHALL 按模式封闭 provider-neutral `PreparedModelCall`/`ModelExecutionPermit` 顺序。Legacy单route逐字保持：immutable route动态hard eligibility → soft policy/fallback/approval与`policy.decision` audit → budget/settlement reservation → deployment Bulkhead permit → lazy client factory取得绑定frozen route的client lease → durable `side_effect_started` mark → adapter send/execute；同route retries继续复用同一permit、reservation、client lease和durable mark。显式route chain唯一执行：immutable chain/candidate动态hard eligibility → candidate独立policy/audit → candidate reservation → durable `attempt_lifecycle=started` identity → deployment Bulkhead permit → candidate-isolated client lease/prepare → send/iterate；每次首次调用或retry都必须追加自己的全局连续、不可覆盖started identity，不得复用candidate/claim聚合mark或前一attempt identity。Vendor client/request对象不得越过adapter boundary，两种模式的client构造均不得联网。

Soft policy deny或approval-required未获批准时，两种模式的目标candidate reservation/permit/client/mark/network均为零；approval只能继续原冻结intent或缩权，不得提高、重置或覆盖shared hard limit。Legacy动态eligibility、Bulkhead、client failure与崩溃场景继续按既有行为处理。Chain在started identity提交后若permit/client/prepare于send前确定失败，MUST在同一owner UoW写`client_not_started` proof并把同一lifecycle原子关闭为`not_started_proven`，再按retry/transfer规则推进；不得删除started identity或声称durable mark为零。Chain started提交后、关闭提交前的崩溃或commit-ack未知，无论permit/client是否取得、`request_sent=false|true`，都 MUST保留current reservation并进入needs-review，不得重取permit/client、重发、创建下一attempt或切换provider。此前`policy.decision` audit只保留原始decision，不得解释为provider已开始。

#### Scenario: Soft policy deny 与未批准零模型副作用
- **WHEN** immutable route或chain candidate hard eligibility已通过，但soft policy deny、要求approval或批准尚未完成
- **THEN** runtime保持目标candidate reservation、permit、client construction/acquisition、durable mark与network delta全部为零；只允许既有`policy.decision` audit记录原始decision
- **AND** approval不得改变owner shared hard limit，只有获准的原intent或更小route才能继续

#### Scenario: Legacy Bulkhead 饱和零网络副作用
- **WHEN** legacy deployment已达到`max_in_flight`且调用在`queue_timeout`内未取得permit
- **THEN** 调用返回`model.bulkhead_saturated`，记录`not_started`，provider call count为零，并释放或取消尚未开始的reservation

#### Scenario: Legacy client lease 构造失败不伪造 provider started
- **WHEN** legacy hard eligibility、policy decision、reservation与Bulkhead permit已完成，但lazy client factory构造client lease失败
- **THEN** runtime不写durable side-effect mark、不发网络；回滚reservation/permit并记录not-started，已部分构造资源被幂等关闭

#### Scenario: Legacy 取得 permit 后、durable mark 前崩溃
- **WHEN** legacy worker已取得permit但尚未写入durable `side_effect_started`时崩溃
- **THEN** permit随进程释放，恢复仍观察到reserved/not_started，可按既有幂等规则安全重放且不会重复计费

#### Scenario: Chain permit、client 或 prepare 在 send 前失败
- **WHEN** chain candidate的独立attempt started identity已提交，随后Bulkhead permit、isolated client lease或prepare在send前以`client_not_started`确定失败
- **THEN** runtime保留该started identity，并在同一UoW追加proof、关闭为`not_started_proven`及完成reservation retry/transfer决定
- **AND** 不得使用legacy“mark为零/删除mark”形状；proof或close提交确认未知时不得推进

#### Scenario: Chain started 后、send 前崩溃
- **WHEN** chain attempt started identity已提交，但进程在permit、client/prepare或send前崩溃
- **THEN** 恢复保留current reservation并进入needs-review，不重取资源、不重发、不创建下一attempt或切换provider

#### Scenario: Durable mark 后、实际 send 前崩溃
- **WHEN** legacy durable `side_effect_started`或chain durable attempt started identity已提交，但进程在adapter send前崩溃
- **THEN** 恢复保守归类为`model.provider_side_effect_unknown`/needs-review，不自动重放、不释放未决reservation；允许假阳性，禁止假阴性重复副作用

#### Scenario: Attempts 之间崩溃
- **WHEN** 一个显式retryable attempt已记录而下一attempt的结果尚未耐久收敛时进程崩溃
- **THEN** legacy按既有started/unknown封闭整个调用；chain只按独立`attempt_lifecycle[]`判断，已存在started identity一律needs-review且不从attempt序号继续请求provider

#### Scenario: 已结算稳定失败的调用重放
- **WHEN** 相同`usage_call_id`重放已经持久化的`model.bulkhead_saturated`、`model.invocation_cancelled`、`model.provider_failed`、`model.provider_retry_exhausted`或`model.provider_side_effect_unknown`结果
- **THEN** runtime的公开调用重放与后台恢复先共用完整settlement validator校验evidence、outcome、稳定error identity与failure/response，再允许发布final event/telemetry或把outbox标记为`published`
- **AND** 合法稳定失败不再次调用provider；冲突或畸形输入统一fail closed，零final publication、零状态推进，不猜测为零调用或返回伪造成功

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

### Requirement: 流式调用复用单一用量证据生命周期
每次流式调用 SHALL 在调用前生成一个稳定 `usage_call_id`，并复用既有 `model_usage` 的 2 槽位 started/final 生命周期。新 `text_stream` 调用的 durable started `ModelUsageEvidence.decision.usage_event_identity` MUST 精确为 `{"ref":"stream-usage","version":"v1"}`，并使用 `usage-stream:{usage_call_id}:s` 与 `usage-stream:{usage_call_id}:f` 作为 started/final event id；usage outbox 绑定 final id。`model.request.started` MUST 在 provider 副作用前发布；成功时 `model.usage.updated` MUST 在 `model.output.completed` 之后且运行终态之前发布。系统 MUST 使用 provider 最终结果携带的可信 usage，不得从 delta 数量、文本长度或零值推断用量。

#### Scenario: 成功流式调用的用量顺序
- **WHEN** provider 完成流式调用并返回可信 usage
- **THEN** started、所有 delta、completed、usage 按此顺序发布
- **AND** usage 与所有流事件具有同一 `usage_call_id`

#### Scenario: 未知结果不伪造用量
- **WHEN** provider 流被中断且无法证明远端停止或取得最终 usage
- **THEN** 系统不发布 `model.usage.updated`
- **AND** 不以 0、估算值、delta 计数或文本长度替代真实 usage

#### Scenario: 历史 usage identity 保持可恢复
- **WHEN** recovery 读取缺少 `stream-usage-v1` 的历史或非流式 durable usage row
- **THEN** 系统继续使用该 row 已绑定的 `usage:{tenant_id}:{usage_call_id}:started|final` identity 补投或校验
- **AND** 不把历史 event 重命名、重键或迁移为 stream identity

### Requirement: 流式结算保持预算与 lease 围栏
流式调用 SHALL 沿用既有预算预留、provider lease 与 attempt evidence。正常完成时，系统 MUST 使用最终 usage 结算预算并释放 lease。已证明停止时，只有 provider-neutral `ModelStreamCloseResult.usage.finality=complete` 且 token 与当前启用 cost 维度全部可信，才可生成中断 `ModelUsageEvidence` 并结算。stopped 或 unknown 的 null/partial usage MUST 在同一 UoW 把 usage outbox、对应 shared-budget claim/allocation 与 owner ledger 提升为 `needs_review`；usage row MUST 保留原 durable started evidence 和一个封闭 `attempt_review`，预算 result MUST 保存同一 review，且两者不得伪造 final evidence。review MUST 精确包含 close state、usage finality、原始受控 outcome/error、安全调用/时延摘要、单个 attempt 和 unknown budget charge；该 charge 的 token/cost 为 null、status 为 unknown、未决 ordinal 为 `[1]`。系统 MUST 保留 stream/usage 容量、reservation 与 lease，拒绝 exact replay 再次调用 provider，不发布 final usage 或 run terminal。不得因已发布部分 delta 就提前结算或释放。

#### Scenario: 正常完成后结算
- **WHEN** completed 与最终 usage 已可靠持久化并发布
- **THEN** 预算按可信 usage 结算且 provider lease 被释放
- **AND** 结算发生在运行终态之前

#### Scenario: 部分 delta 后状态未知
- **WHEN** 已发布部分 delta 但 provider 结果未知
- **THEN** 预算 reservation 与 provider lease 保持未决
- **AND** attempt evidence 记录 `side_effect_state=unknown`，运行终态被阻止

#### Scenario: stopped usage 维度不完整
- **WHEN** close result 为 stopped，但 token 任一项未知或 cost-enabled route 缺少可信 cost
- **THEN** observed 值只进入 attempt evidence，调用级 budget charge 保持 unknown
- **AND** usage outbox、预算 operation 与 owner ledger 在同一事务进入 needs_review
- **AND** 系统不发布最终 usage、不释放 stream/usage 容量或 lease、不允许运行终态

#### Scenario: needs-review exact replay 不重启供应商
- **WHEN** 同一稳定流式调用再次命中已持久化的 attempt review
- **THEN** 系统逐值校验 usage 与预算保存的是同一 review
- **AND** 返回 needs-review 重放错误，不再次 prepare、迭代或重启 provider stream

### Requirement: 用量证据绑定 route chain 和最终候选
多候选模型调用 SHALL 保留既有 `decision.route/attempts[]/budget_charge`，并在 started/final/failure evidence 的 decision 中增加唯一 exact `route_chain` 字段：`schema_version="model-route-chain-evidence-v1"`、`identity=<完整 model-route-chain-v1>`、`state=<完整 model-route-chain-state-v1>`，不得把三个 schema 名称互换。started 与 final/failure identity 必须逐值相同，`decision.route` 和顶层 provider/model 必须逐值命中每份 state 始终非空的 `evidence_route_ordinal`；completed 等于 selected，cancelled等于唯一cancelled candidate且selected为空，denied 等于唯一 denied candidate，exhausted 等于最后 cause，unknown 等于最后 active/unknown candidate，尚未激活等于当前评估 candidate。unknown 只进入私有 review，不发布 final usage。state MUST 完整包含 candidate count、64位小写 SHA-256 `usage_call_id`、operation identity digest、互斥 active/waiting-approval/selected ordinal、evidence route ordinal、delta fence、current reservation、逐候选去敏 route/state/聚合 `side_effect_state`/reason/request-sent/http-response-observed/http-status/response-identity-observed/usage-observed/text-observed/delta-observed/completion-observed、有序 `not_started_proofs[]`、approval-request-binding/approval-grant-binding digest，以及从 1 连续的 reservation transitions；只公开 digest，不公开 approval id/lease id。Candidate state enum精确为 `pending|static_ineligible|budget_ineligible|waiting_approval|active|not_started|completed|cancelled|unknown|denied`；`static_ineligible`只允许 `reason=static_ineligible`，`budget_ineligible`只允许 `reason=soft_budget|balance`，两者均须零 attempt、零 proof、零 reservation与全 false观察事实。Static/soft-budget/普通 balance skip的两个 approval binding均为空且不为该候选追加 transition；只有获批 activation的 balance skip强制两者同时非空，禁止单边 binding或后继复用，并可保留既有 waiting coordination及后续零释放 `transferred/balance`，但不得出现为该候选激活 reservation 的 transition。`not_started`只允许完整 proof覆盖的实际 attempts。`cancelled`只允许`reason=invocation_cancelled/side_effect_state=result_committed/completion_observed=false`、selected/active/waiting为空、current reservation为canonical空且evidence route ordinal命中本candidate，不新增transition。调用级 claim 的 started 是整链高水位，不得覆盖当前 candidate 或 attempt 的独立事实；合法的跨候选及同候选 retry 混合 proof 顺序必须逐 attempt 保真。

完整state还 MUST包含按全链global attempt从1连续且不可覆盖的`attempt_lifecycle[]`。每项exact fields为attempt、candidate ordinal、`model-route-attempt-identity-v1` digest、`lifecycle_state=started|not_started_proven|unknown|settled`、side-effect/request/HTTP response/status/response identity/usage/text/delta/completion观察事实与nullable proof digest。Started record必须先于client/send/provider边界耐久创建；identity字段不可改。`not_started_proven`必须与candidate proof及公开`attempts[]`同attempt逐值一致，unknown/settled不得带proof；终态不得降级。任一已存在started record，即使`request_sent=false`，都禁止恢复重发；缺失、重复、重排、跨candidate绑定、非法状态迁移或attempt-start/close commit-ack冲突必须在started/settlement/publication/recovery统一关闭失败并保留未决reservation。

可信actual取消要求matching lifecycle为`settled/side_effect_state=result_committed/completion_observed=false`，公开attempt为`outcome=cancelled/side_effect_state=started`并逐值保存完整close usage；budget charge以actual结算，error code为`model.invocation_cancelled`，不允许response、selected、completed candidate或`model.output.completed`。`prepared`不构成request/provider调用事实；`provider_called`只由明确request、HTTP response、result、usage、text或delta观察推导，本地cleanup失败不得覆盖已形成的稳定安全错误。

Validator还 MUST锁定chain调用顺序为reservation→attempt started identity→permit→client/prepare→send/iterate；permit/client/prepare失败的公开attempt可保持`request_sent=false`，但必须存在同一started identity及原子`not_started_proven`关闭。Legacy单route evidence不新增该字段且原顺序不变。

每个实际 chain attempt 完整继承 5.29 exact fields，并强制增加 candidate ordinal/deployment/provider/model、request-sent/HTTP-response-observed/nullable HTTP status、response-identity/usage/text/delta observed 四个 bool、nullable completion-observed、nullable not-started reason/proof digest、endpoint-policy digest与 nullable classifier ref/version；attempt number 跨候选与同候选 retry 全局连续，没有 route-chain schema 时这些字段都属于 unknown fields。每个零 charge attempt 必须与 candidate `not_started_proofs[]` 中同全局 attempt 的 record 逐值相等并可机械重算 digest；非 not-started attempt 的 reason/digest 必须为 null。budget charge 按全链 attempt 聚合。公开 `model.request.started` 在 provider 副作用前发布，本身不固定最终 route；started/final provider/model 不同时，只能由同一 chain逐项证明全部前驱：静态或预算候选分别是零 attempt的 `static_ineligible|budget_ineligible` state，运行时候选的全部实际 attempts以完整 proof list和连续 transition安全收敛。除逐 attempt 完整证明的受信业务未开始特例外，provider started 或 HTTP response 后 route 不得变化，denied/exhausted 的 selected 必须保持 null。

#### Scenario: 第二候选完成
- **WHEN** 第一候选在 client/send 前以 `client_not_started` 收敛，第二候选完成并返回可信 usage
- **THEN** final evidence 选择 ordinal 2，第一候选 charge 为 0，第二候选记录 actual charge
- **AND** started/final 的 chain id 相同

#### Scenario: selected route 与 final response 不一致
- **WHEN** final provider/model 或 price identity 不匹配 selected candidate
- **THEN** settlement validation 拒绝，usage outbox 不发布且 terminal 保持围栏

#### Scenario: route-chain evidence 形状非法
- **WHEN** identity/state schema 互换，attempt 在第二候选重置，route 字段缺失，proof list 覆盖/缺失/重排或与 attempts 观察字段不一致，或出现 unknown field、重复 ordinal、非法 state/reason/transition、bool 数字、负数、NaN/Inf
- **THEN** started/settlement/publication/recovery validator 统一关闭失败
- **AND** 不发布 final usage、completed 或 terminal

#### Scenario: 可信actual取消可重放但不伪造完成
- **WHEN** route-chain stream的显式取消已以完整可信stopped usage耐久结算，随后公开调用或后台恢复重放同一usage call
- **THEN** validator接受唯一cancelled candidate、settled lifecycle、cancelled attempt、actual charge与`model.invocation_cancelled`，selected/active/waiting保持为空
- **AND** 重放不调用provider、不发布`model.output.completed`；usage不完整、durable delta不确定或任一状态/evidence映射冲突都转为needs-review并保留围栏

#### Scenario: approved transition 必须唯一且可重放
- **WHEN** matching grant把waiting candidate成功激活
- **THEN** evidence只接受同ordinal的`state=approved/reason=approval_granted`、released=`0/null`、reserved=目标冻结bound这一条transition，candidate在同一UoW直接为active
- **AND** 重复`activated`、from/to不同、bound漂移、余额不足分支仍出现approved，或commit-ack replay追加/改写transition时，SQLite/PostgreSQL settlement/publication/recovery validator统一关闭失败且不调用provider

#### Scenario: 零impact source anchor 的后继 policy 分支完整
- **WHEN** 获批candidate因balance不足成为零impact source anchor，或初始扫描只有前导普通skip，随后后继独立policy返回allow、require-approval或deny
- **THEN** evidence按canonical source规则唯一接受balance-anchor→active/waiting/denied/exhausted tuple；初始路径只接受null→active、null→waiting或deny零transition，普通skip不得成为source
- **AND** 缺失、额外或把zero bound改成current bound的transition在started/settlement/publication/recovery统一关闭失败，双数据库不得各自选择默认tuple

### Requirement: unknown chain 不伪造 final usage
任一 active candidate 为 started/unknown，或已经观察 response/usage/text/delta但无法可信结算时，usage outbox、shared-budget claim 与 owner ledger MUST 在同一 UoW 进入 needs-review。已有 not-started candidates 和当前 attempt MAY 进入去敏 attempt review，但系统 MUST NOT 发布 final `model.usage.updated`、退款、把缺失维度记零或调用后续 provider。

#### Scenario: 首选 unknown
- **WHEN** 首选 read timeout 且没有可信停止证明
- **THEN** evidence 保留 active ordinal、unknown attempt 与原 reservation
- **AND** exact replay 不再次调用首选或任何后继

#### Scenario: 观察 usage 后本地失败
- **WHEN** provider usage 已观察但本地 settlement/publication 失败
- **THEN** 调用按 unknown/needs-review 保留已知事实与未决维度
- **AND** 不把已观察 usage 当作 not-started 切换授权
