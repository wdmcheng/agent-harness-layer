# policy-gated-tool-loop Specification

## Purpose
TBD - created by archiving change policy-gated-tool-loop. Update Purpose after archive.
## Requirements
### Requirement: Runtime 通过绑定 façade 执行固定模型工具循环
Runtime SHALL 通过绑定到 tenant/run/agent/request/trace 的 `BoundModelToolLoopService.run(request: ModelRequest, *, operation_key: str, tool_selection: ToolCatalogSelection | None = None, limits: ModelToolLoopLimitOverrides | None = None)` 执行工具循环。业务 executor 只能提交初始 model request、稳定语义 operation key，以及 `provider-neutral-tool-intent` capability 定义的 exact `ToolCatalogSelection` 和本 change exact `ModelToolLoopLimitOverrides` 两个独立可选 DTO；它 MUST NOT 扩展 `ModelRequest`，也不得提交 loop/turn/tool/usage identity、Registry handler、policy decision、approval grant 内容或动态余额。每轮顺序 MUST 固定为 `model turn → ToolIntent validation → Registry resolve → policy/audit → optional approval/checkpoint → execution claim → tool execution → output guard → ContextAssembler → next model turn`。Tool-intent protocol 的唯一成功终态为 `final_text`；`final_structured` MUST 作为 cross-capability protocol violation 结算已发生模型影响并关闭失败，不得进入 loop terminal success。

#### Scenario: 单个工具后得到最终文本
- **WHEN** 第一轮产生合法 intent、policy allow、工具完成且第二轮产生 `final_text`
- **THEN** loop 按固定顺序执行并只返回第二轮最终文本
- **AND** handler 与每个 model turn 各按合同调用一次

#### Scenario: 结构化结果不能结束工具循环
- **WHEN** tool-intent provider 在任一轮返回 `final_structured`
- **THEN** runtime 按 cross-capability protocol violation 关闭失败并结算已发生的 model usage
- **AND** 不执行工具、不返回 structured success，调用方必须改用独立 structured-output seam

#### Scenario: 非法跳步关闭失败
- **WHEN** caller 或恢复状态试图从未解析 intent 直接执行、跳过 policy、跳过 ContextAssembler 或重复 turn ordinal
- **THEN** runtime 在下一外部副作用前以稳定 transition/identity 错误关闭

### Requirement: Policy 三态严格控制工具副作用
Registry resolve 成功后，runtime SHALL 使用 `ResolvedToolIntent` 绑定的 action/resource 与受信 identity/context 调用 PolicyEngine。`deny` MUST 保持零 execution claim/handler 副作用；`require_approval` MUST 只创建 durable waiting approval/checkpoint；只有 `allow` 或 matching approved continuation MAY 进入 execution claim。

#### Scenario: Deny 不执行工具
- **WHEN** policy 对已解析工具返回 `deny`
- **THEN** runtime 写脱敏 policy/audit evidence并结束当前 loop 为确定失败
- **AND** execution claim、handler、MCP/shell/file/network 调用计数均为零

#### Scenario: Require approval 只进入等待
- **WHEN** policy 返回 `require_approval`
- **THEN** runtime 创建绑定原 loop/turn/tool call 的 approval/checkpoint并返回 waiting
- **AND** handler、下一 model turn 与 run terminal均不发生

#### Scenario: Allow 恰好执行一次
- **WHEN** policy 返回 `allow` 且所有 hard bounds/capacity/claim 成功
- **THEN** runtime 调用对应 handler 恰好一次并记录工具 evidence

### Requirement: 工具结果作为不可信来源回注下一轮
每个 `ToolCallResult` SHALL 先完成 secret redaction、size guard、truncation/artifact materialization 和 injection inspection，再转换为 `ContextFragment(kind=tool_result, trust_level=untrusted)`。Fragment MUST 保留 source ref、artifact ref、token estimate、truncation 与 injection summary；下一模型轮 SHALL 只消费 ContextAssembler 的冻结 output ref/digest 和安全 assembled text，工具内容 MUST NOT 覆盖 system/developer/policy 指令。

#### Scenario: 指令型工具输出不提权
- **WHEN** 工具结果包含 system override、policy bypass 或 developer instruction 文本
- **THEN** ContextAssembler 将其保留为 untrusted 引用并记录 injection summary
- **AND** 下一轮高优先级指令与 policy 绑定保持不变

#### Scenario: 大输出和 secret 不进入 prompt/event
- **WHEN** 工具结果超过 inline 上限或包含 token/password/cookie
- **THEN** inline fragment/event/audit 只保留脱敏截断摘要和 artifact ref
- **AND** 原始 secret 与完整大 payload 不进入 model request

### Requirement: 循环 hard bounds 在启动时冻结且只能缩小
Loop SHALL 只从绑定 Agent descriptor 的 exact `model_tool_loop` summary 取得五项权威 maxima，并在首次 model/provider/tool 副作用前以受信 `loop_started_at + max_duration_seconds` 推导 absolute wall-clock deadline，同时冻结 `max_turns`、`max_total_tokens`、nullable `max_total_cost_usd`、`max_tool_output_bytes`、deadline 与 tool catalog identity。Exact `ModelToolLoopLimitOverrides` 只允许五个全部必填 nullable 字段：`max_turns`、`max_total_tokens`、`max_total_cost_usd`、`max_tool_output_bytes`、`max_duration_seconds`；null 表示继承对应 Agent maximum，非 null 必须通过同类型/范围校验且不得大于对应 maximum。DTO 缺省等价于五项全继承；未知/缺失字段、bool、NaN/Infinity、负数、扩大值或任意字典输入 MUST 在 claim、reservation、client、provider 和工具副作用前拒绝。Approval、config reload、resume 或下一轮 MUST NOT 提高、重置或重新解释任一冻结上限；request wall-clock 值只能缩短 duration，不能提交 absolute deadline 或启动时间。每轮 model reservation SHALL 同时受 root shared budget、loop 剩余量和单轮 route bound约束。

#### Scenario: Turn 上限阻止下一模型轮
- **WHEN** 已完成的 turn ordinal 达到 `max_turns` 且没有 `final_text`
- **THEN** runtime 以 `model.tool_loop_limit_exceeded` 确定终止
- **AND** 不再调用 model、Registry 或工具

#### Scenario: Approval 不重置 deadline 或余额
- **WHEN** loop 在审批等待后恢复且 wall-clock deadline或预算余额已缩小
- **THEN** runtime 复用原冻结deadline/累计usage并在副作用前重检当前hard bounds
- **AND** 不因等待或grant提高上限

#### Scenario: 覆盖缺省与 null 逐项继承
- **WHEN** caller 不提供 `limits`，或提供五项均为 null 的 exact overrides
- **THEN** loop 逐值冻结绑定 descriptor 的五项 maxima 并从受信启动时间推导 deadline
- **AND** 不读取 deployment、环境、动态余额或代码默认值

#### Scenario: 覆盖只能逐项缩小
- **WHEN** caller 提供合法非 null 覆盖值
- **THEN** 每项 effective bound 为对应 Agent maximum 与覆盖值中的较小值
- **AND** snapshot、approval、resume 与 evidence 绑定同一 effective bounds 和 absolute deadline

#### Scenario: 非法覆盖零副作用拒绝
- **WHEN** overrides 缺字段、含额外字段、bool、NaN/Infinity、负数、超过 Agent maximum，或 caller 自报 absolute deadline/started_at
- **THEN** bound seam 在 usage claim、reservation、client、provider、tool claim 和 handler 前关闭失败

#### Scenario: 工具输出上限在下一轮前执行
- **WHEN** guarded tool result 仍超过冻结的 loop output byte/token 上限
- **THEN** runtime 在 Context Assembly 或下一 model call 前截断到合同允许范围或确定失败
- **AND** 不静默扩容

### Requirement: 受控工具循环不把不确定中断自动恢复为重试
`durable-tool-loop-resume` capability 接入前，受控工具循环遇到 provider/tool/result/context/event/commit outcome 无法证明的中断时 SHALL 保持 waiting 或进入 `needs_review`，并 MUST NOT 自动重调 provider、handler 或下一 turn。确定性完成路径和现有 approval exact result MAY 继续；不确定路径 MUST 阻止 run terminal。

#### Scenario: Handler 后结果未耐久不重试
- **WHEN** handler 可能已执行但 result/claim commit acknowledgement未知
- **THEN** loop 进入 needs-review且不再次调用 handler或model

#### Scenario: Context publication 未知不生成下一轮
- **WHEN** Context Assembly result 已写但 event/outbox确认未知
- **THEN** runtime 不从当前 payload重组或启动下一 model turn
- **AND** 保留 terminal fence供`durable-tool-loop-resume`恢复
