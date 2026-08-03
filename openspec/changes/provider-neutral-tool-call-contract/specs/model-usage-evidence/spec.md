## MODIFIED Requirements

### Requirement: 真实非流式模型调用以调用级 reservation 和逐 attempt 证据收敛
每次受控真实模型调用 SHALL 在既有稳定 `usage_call_id` 和 durable settlement 内只建立一个调用级 reservation；显式 route chain 在任一时刻也只能有一个候选 reservation，并在 approval waiting 时使用零 impact coordination row。`ModelUsageEvidence.decision` SHALL 保存冻结 route、调用级预算上界及有序 attempt summaries；chain mode 还必须保存 exact `route_chain` identity/state。真实 deployment 的 allowed model MUST 只引用 `ModelSettings.model_catalogs` 中 exact canonical ref/version；`config/model_catalog.py` 按 capability 解析并校验封闭联合：text/stream/structured 只接受 `model-catalog/v1 + single-user-text-no-tools/v1`，singleton tool-intent deployment只接受 `model-catalog/v2 + single-user-text-with-tool-catalog/v1 + max_tool_catalog_utf8_bytes`，两者都只使用 `utf8-bytes-plus-envelope/v1`、受信 envelope、价格、price-source identity 与 canonical digest。Deployment、Agent、request 或 provider 返回值不得自证、覆盖或补齐这些权威值。

Route 计划 MUST 先拒绝超过 deployment `max_prompt_utf8_bytes` 的 prompt；v1 route 用 checked arithmetic 计算 `trusted_input_token_bound=len(prompt UTF-8 bytes)+input_envelope_token_bound`，v2 tool-intent route 还必须先验证选定 `provider-tool-catalog-v1` canonical bytes 不超过 catalog max，再计算 `trusted_input_token_bound=len(prompt UTF-8 bytes)+len(provider tool catalog canonical UTF-8 bytes)+input_envelope_token_bound`。真实 request 的 `max_output_tokens` MUST 在 `1..deployment.max_output_tokens` 内并冻结为 adapter 不可放大的 `output_token_cap`。Deployment 静态 max token/cost ceiling MUST 先按 typed-config 的 v1/v2 capability-specific 公式验证；每个 candidate 的动态 `per_attempt_token_bound` MUST 等于 `trusted_input_token_bound+output_token_cap`，cost 启用时动态 `per_attempt_cost_bound` MUST 等于 `trusted_input_token_bound*input_token_price_usd+output_token_cap*output_token_price_usd` 的有限非负 Decimal 结果，且两项价格、非空 price-source identity 与 cost bounds 必须一致冻结；cost disabled 时两项价格、price-source identity 和全部 cost bounds 只能按 typed catalog 的禁用语义为 null，不得被解释为零价或沿用陈旧来源。

Catalog schema/ref/version/provider/model/request-shape/strategy/price/source/digest 任一未知、不匹配或判别混搭，deployment 复制值低报或高报，静态 ceiling 低报或高报，动态公式不一致，prompt/output/tool catalog 越界，非有限或溢出时 MUST 在 reservation、Bulkhead、client lease 获取/构造和 provider 前返回 `config.invalid` 或 `budget.reservation_rejected`，且本调用 reservation count、client-construction delta、network/provider call count 均为零。每个候选 reservation MUST 再以该候选动态 `per_attempt_bound * max_attempts` 的 checked arithmetic 计算；candidate transfer 只能原子替换，不能叠加。Tool-intent route 固定 `max_attempts=1` 且没有 candidate transfer、response retry 或 classifier。Route、budget snapshot、operation identity 与公开 evidence MUST 冻结相同 catalog ref/version/digest 和解析值；tool-intent 私有 snapshot还必须保存完整 canonical provider catalog bytes与tool request identity。Reload 只影响新 root，恢复不得读取 current catalog/Registry 补齐、改价或重建工具定义。Adapter MUST 把冻结 `output_token_cap` 作为 Pydantic AI `ModelSettings(max_tokens=...)` 传给真实 provider，不得从 request/settings 重算或放大。Summary MUST 只包含 API Contract 5.29 与 capability delta 规定的脱敏字段，MUST NOT 包含 prompt、schema definition、response raw body、header、credential、完整 URL、SDK exception 或 SDK object。

每个 attempt 的 token/cost 维度 SHALL 分别按以下穷举矩阵处理：可证明 `side_effect_state=not_started` 且 request/HTTP response 均未发生的 `client_not_started` attempt 为零；显式 chain 中 claim/candidate 可保持 started，但由端点绑定 classifier、冻结跨 provider 状态白名单、无 response identity/usage/text/delta 和该全局 attempt 的 durable proof record共同证明的 `trusted_business_not_started` attempt也为零，且不得回写 request/response/started 历史；`side_effect_state=started` 且有该维度可信 provider usage/cost 时纳入 actual 聚合；其他 `side_effect_state=started` 且任一已启用维度无可信 actual 时，无论 outcome 是 `completed`、确定性 `failed` 还是 `retryable_status`，整个调用都必须保持当前 reservation、进入 `needs_review` 并阻止 terminal，不得用 attempt 上界替代 actual、不得退款。上述 `trusted_business_not_started` 零 charge 特例只属于显式 text route chain；tool-intent send 后不得消费 classifier或把响应降格为未开始。Chain 路径要求 same-route retry 前已原子持久化上一 attempt proof，最终 transfer 时 candidate proof list 覆盖全部实际 attempts且与 evidence 逐值一致；legacy 同 route retry即使有相同header也继续按既有未决规则处理。`side_effect_state=unknown` 或完成状态不明时遵循相同未决规则。Cost disabled 时该维度不参与 reservation/needs-review，但不得影响 token 维度判断。`completed+not_started`、proof 缺失/覆盖/重排等不可能组合、actual 超过 reservation 或 evidence 自相矛盾时 MUST fail closed 为 `needs_review`，impact 遵循 shared-parent-budget-ledger 的 `max(original_reservation,trusted_actual)`，不得增加可用余额。

最终成功前的失败 attempts MUST 与成功 attempt 一并聚合。只有每个 started attempt 都有各启用维度可信 actual，且没有 unknown/invalid/actual-over，才能用聚合 actual 原子替换当前 reservation、退还差额并允许 completed 或确定性 failed terminal；否则当前 reservation 或更高可信 actual impact 保持未决，owner 的新预算 operation 与 terminal 均被阻止。顶层 token 字段仅在全部 started attempts 都有可信 token actual时等于其总和，否则为 null；cost 全部可信时按 reported/estimated 规则聚合，否则为 null/`unavailable`。已知的部分 actual 可保留在 attempts evidence 中，但不得据此释放未决维度。禁止用零、attempt 上界或最终一次 usage 冒充调用总消耗。

#### Scenario: 首次 attempt 成功
- **WHEN** provider 在首次非流式请求中返回合法 capability 结果和完整 usage
- **THEN** 最终 evidence 记录一个 completed attempt、真实 latency/token/cost、同一冻结 route 和等于实际值的 budget charge，settlement/terminal 顺序保持不变

#### Scenario: Tool-intent actual catalog bytes 进入预约
- **WHEN** singleton tool-intent deployment 选择未超限的 provider tool catalog 并完成一次调用
- **THEN** reservation 使用 prompt、实际 canonical catalog bytes、envelope 与 output cap，started/final evidence绑定同一 v2 catalog和tool request identity
- **AND** `max_attempts=1`，不创建route-chain、retry或classifier proof

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
- **AND** legacy 单 route 与 tool-intent route 的相同 header仍保持原调用 reservation并进入 needs-review，不获得该 chain-only 例外

#### Scenario: Chain candidate 多次受信 retry 的逐 attempt 结算
- **WHEN** 同一 chain candidate 连续两个 attempts 都以完整 `trusted_business_not_started` 收敛，第一条按冻结策略触发同 route retry，第二条耗尽后触发 transfer
- **THEN** 两个全局 attempt 各有不可覆盖的 proof record、attempt evidence 和零 charge，candidate 聚合高水位保持 started
- **AND** SQLite/PostgreSQL recovery 只从下一尚未开始的 attempt 或已提交 transfer 继续；任一 record 缺失、覆盖、重排、字段冲突或提交确认未知都进入 needs-review且不重放 provider

#### Scenario: 已完成结果但 provider 未返回 usage
- **WHEN** attempt 已返回完整 text/tool-intent结果并确定 `completed`，但没有可信 token/cost usage
- **THEN** 已知 provider-neutral result 可写入耐久结果，但原调用 reservation 保持未决，顶层缺失维度为 null，settlement/owner 进入 `needs_review` 并阻止 terminal或工具执行，不用估算或上界冒充 actual

#### Scenario: 确定性失败但 provider 未返回 usage
- **WHEN** attempt 已开始并以不可重试 HTTP/adapter 错误确定性失败，但没有可信 token/cost usage
- **THEN** provider outcome 以 `model.provider_failed` 记录，但原调用 reservation 保持未决，settlement/owner 进入 `needs_review` 并阻止 failed terminal；只有可证明整个调用没有 provider 副作用时才可释放

#### Scenario: Evidence 组合非法
- **WHEN** attempt 声称 `completed+not_started`、unknown 却携带互相冲突的完成状态、started/response 后缺少完整 chain-only trusted proof却伪造零 charge，tool-intent伪造classifier proof，或 charge 与 usage availability 不符合穷举矩阵
- **THEN** settlement fail closed 为 needs-review，保留原 reservation 或更高可信 actual impact、阻止 terminal，SQLite 与 PostgreSQL 不得自行选择默认退款规则

#### Scenario: Reservation 上界无效
- **WHEN** prompt/output/catalog cap 越界、输入 strategy 未受 catalog 认证、v1/v2判别混搭、任一 candidate 的每 attempt 上界低报或高报、token/cost 公式不一致、`per_attempt_bound * max_attempts` 溢出/非有限/超出共享预算，或 cost hard limit 下缺少可信价格
- **THEN** 调用以 `budget.reservation_rejected` 在 reservation、Bulkhead、durable side-effect mark、client lease 获取/构造和 provider call 前失败，本调用 reservation count、client-construction delta、network/provider call count 均为零，且 cost 不被当作 0

#### Scenario: 冻结输出 cap 被 adapter 强制执行
- **WHEN** 合法真实 candidate 形成 `output_token_cap` 并进入 Pydantic AI adapter
- **THEN** provider double 观察到 `Agent.run(..., model_settings=ModelSettings(max_tokens=output_token_cap))`，其值与 route/evidence 逐字相同；adapter 不读取 mutable request/settings 放大 cap

## ADDED Requirements

### Requirement: 工具意图模型轮使用统一模型用量证据
模型输出工具意图时 SHALL 继续使用稳定 `usage_call_id`、route/attempt、shared-budget reservation、started/final evidence 与 exact/conflict replay。Final evidence SHALL 绑定 turn kind、catalog digest、tool intent digest 和 nullable tool call identity；它 MUST NOT 保存原始 arguments、prompt、SDK object 或 tool output。工具尚未执行 MUST NOT 把已发生的 model token/cost/latency 记为零。

#### Scenario: Intent 成功保存 model usage
- **WHEN** 一轮模型调用产生合法 `ToolIntent`
- **THEN** final usage 绑定 intent/catalog digest并结算全部实际 provider attempts
- **AND** tool execution count 为零不改变 model charge

#### Scenario: Intent identity conflict 零 provider 重放
- **WHEN** 相同 usage call id 携带不同 catalog、turn kind、tool name、arguments digest 或 schema identity
- **THEN** runtime 以 replay conflict 拒绝且不再次调用 provider

#### Scenario: Intent 未知保留用量围栏
- **WHEN** provider result、attempt usage、cleanup 或 durable commit 状态未知
- **THEN** usage/shared-budget 进入 needs-review并保留 reservation
- **AND** 不发布 final intent、不授权后续工具执行

### Requirement: 工具目录字节完整进入可信输入与耐久预算身份
工具意图 route SHALL 从受信 `model-catalog/v2` 取得 `single-user-text-with-tool-catalog/v1`、`utf8-bytes-plus-envelope/v1`、非负 `max_tool_catalog_utf8_bytes`、envelope 与价格。选定 `provider-tool-catalog-v1` 的实际 canonical UTF-8 bytes MUST 不超过该上限，`trusted_input_token_bound` MUST 精确等于 prompt UTF-8 bytes、实际 catalog bytes 与 envelope bound 之和；静态 deployment ceiling SHALL 使用 catalog max，动态 per-attempt token/cost 与调用级 reservation SHALL 使用实际 catalog bytes并执行 checked arithmetic。

`tool-intent-request-identity-v1` SHALL 是 exact object，字段固定为 `schema_version/request_shape_ref/request_shape_version/model_catalog_digest/tool_catalog_digest/tool_catalog_utf8_bytes/max_tool_catalog_utf8_bytes/trusted_input_token_bound/output_token_cap`，整数拒绝 bool，digest 为 64 位小写 SHA-256，并使用 provider catalog 的 canonical JSON 规则计算 `tool_request_identity_digest`。既有私有 route/budget snapshot MUST 保存完整 provider catalog canonical bytes 与该 identity，公开 route/evidence MUST 只投影 identity/digest；operation identity、started/final evidence 与 exact replay SHALL 逐值绑定它。`tool-intent-policy-approval-arguments-v1` 瞬时 exact preimage SHALL 只含 schema version、usage call id、operation identity digest、既有 request arguments hash 与 tool request identity digest；continuation/checkpoint/grant MUST 保存并交叉校验两个 digest。恢复 MUST NOT 从 current config 或 Registry 补值。

#### Scenario: 实际目录字节进入每次调用预约
- **WHEN** 合法工具启用 route 选择一个未超限的工具子目录
- **THEN** per-attempt token/cost 与调用级 reservation 使用该子目录实际 canonical bytes
- **AND** route、snapshot、operation 与 usage evidence 保存同一 request/catalog/bound identity

#### Scenario: 超长 schema 在 provider 前关闭
- **WHEN** 单个 schema 或目录组合使 canonical bytes 超过 `max_tool_catalog_utf8_bytes`
- **THEN** runtime 在 usage claim、client 和 provider 前以稳定错误零调用拒绝

#### Scenario: Approval 与 replay 不接受预算身份漂移
- **WHEN** approved continuation 或 exact replay 的 shape、catalog bytes/digest/max bound、可信输入上界或价格身份与首次耐久值不同
- **THEN** runtime 在 provider 前关闭失败并保留原 owner budget 事实
